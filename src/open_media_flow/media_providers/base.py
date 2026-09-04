from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..models import AssetKind


class MediaGenerationError(RuntimeError):
    pass


class MediaRuntimeUnavailableError(MediaGenerationError):
    """The provider could not be reached; the existing job may still be running."""


@dataclass(frozen=True)
class GenerationRequest:
    task_id: str
    shot_id: str
    kind: AssetKind
    prompt: str
    negative_prompt: str = ""
    duration_seconds: int = 5
    width: int = 512
    height: int = 896
    seed: int = -1
    reference_image_path: str | None = None
    workflow_variant: str | None = None


@dataclass(frozen=True)
class MediaJob:
    id: str
    provider: str


@dataclass(frozen=True)
class MediaJobStatus:
    state: str
    progress: int = 0
    media_path: Path | None = None
    error: str | None = None


class MediaProvider(Protocol):
    name: str

    def available(self, kind: AssetKind) -> bool: ...
    def submit(self, request: GenerationRequest) -> MediaJob: ...
    def poll(self, job_id: str, kind: AssetKind) -> MediaJobStatus: ...
