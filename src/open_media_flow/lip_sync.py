from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class LipSyncError(RuntimeError):
    pass


class LipSyncRuntimeUnavailableError(LipSyncError):
    pass


@dataclass(frozen=True)
class LipSyncJob:
    id: str
    provider: str


@dataclass(frozen=True)
class LipSyncJobStatus:
    state: str
    progress: int = 0
    stage: str | None = None
    elapsed_seconds: int | None = None
    media_path: Path | None = None
    sync_score: float | None = None
    face_coverage: float | None = None
    error: str | None = None


class LocalLipSyncClient:
    """Client contract for a future local MuseTalk or LatentSync runtime.

    The control plane can ship before model weights are installed. When the
    runtime is disabled or absent, the orchestrator records a truthful
    narration fallback instead of claiming that lip-sync was performed.
    """

    name = "local-lip-sync"

    def __init__(self, base_url: str, api_key: str, media_root: Path):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.media_root = media_root.resolve()

    def available(self) -> bool:
        request = urllib.request.Request(
            f"{self.base_url}/health", headers=self._headers()
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                result = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and result.get("lip_sync_ready") is True
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return False

    def submit(
        self,
        task_id: str,
        shot_id: str,
        video_path: str,
        audio_path: str,
    ) -> LipSyncJob:
        payload = {
            "task_id": task_id,
            "shot_id": shot_id,
            "video_path": self._request_path(video_path),
            "audio_path": self._request_path(audio_path),
        }
        request = urllib.request.Request(
            f"{self.base_url}/v1/video/lip-sync",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(json_content=True),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LipSyncError(f"lip-sync runtime returned HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise LipSyncRuntimeUnavailableError(
                "lip-sync runtime is unreachable"
            ) from exc
        job_id = str(result.get("job_id") or "")
        if not job_id:
            raise LipSyncError("lip-sync runtime did not return job_id")
        return LipSyncJob(
            id=job_id,
            provider=str(result.get("provider") or self.name),
        )

    def poll(self, job_id: str) -> LipSyncJobStatus:
        request = urllib.request.Request(
            f"{self.base_url}/v1/video/lip-sync/{job_id}",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LipSyncError(f"lip-sync status returned HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise LipSyncRuntimeUnavailableError(
                "lip-sync runtime status is unreachable"
            ) from exc
        media_path = None
        if result.get("relative_path"):
            media_path = self._response_path(str(result["relative_path"]))
        return LipSyncJobStatus(
            state=str(result.get("state") or "processing"),
            progress=max(0, min(100, int(result.get("progress") or 0))),
            stage=str(result.get("stage") or "") or None,
            elapsed_seconds=(
                max(0, int(result["elapsed_seconds"]))
                if result.get("elapsed_seconds") is not None
                else None
            ),
            media_path=media_path,
            sync_score=self._score(result.get("sync_score")),
            face_coverage=self._score(result.get("face_coverage")),
            error=str(result.get("error") or "") or None,
        )

    def _request_path(self, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute():
            return value
        try:
            return path.resolve().relative_to(self.media_root).as_posix()
        except ValueError as exc:
            raise LipSyncError("lip-sync input is outside the shared media root") from exc

    def _response_path(self, value: str) -> Path:
        candidate = (self.media_root / value).resolve()
        try:
            candidate.relative_to(self.media_root)
        except ValueError as exc:
            raise LipSyncError("lip-sync runtime returned an unsafe path") from exc
        if not candidate.is_file():
            raise LipSyncError("lip-sync output file is missing")
        return candidate

    @staticmethod
    def _score(value: object) -> float | None:
        if value is None:
            return None
        return max(0.0, min(1.0, float(value)))

    def _headers(self, *, json_content: bool = False) -> dict[str, str]:
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers
