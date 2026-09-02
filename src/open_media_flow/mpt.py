from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .models import ContentTask


@dataclass(frozen=True)
class VideoJobStatus:
    state: int
    progress: int
    media_path: Path | None = None
    error: str | None = None


class MoneyPrinterClient:
    def __init__(
        self,
        base_url: str,
        media_root: Path,
        api_key: str = "",
        output_root: Path | None = None,
    ):
        self.base_url = base_url
        self.media_root = media_root.resolve()
        self.api_key = api_key
        self.output_root = (output_root or media_root.parent / "output/video-engine").resolve()

    def _headers(self, *, json_content: bool = False) -> dict[str, str]:
        headers = {"Content-Type": "application/json"} if json_content else {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _material_payload(self, task: ContentTask) -> list[dict[str, str]]:
        requested = list(task.video_materials)
        if not requested and task.media_path:
            requested.append(task.media_path)
        if not requested:
            raise ValueError(
                "video_materials is empty; add files under data/inbox and pass their paths"
            )

        materials = []
        for value in requested:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = self.media_root / candidate
            resolved = candidate.resolve(strict=True)
            try:
                relative = resolved.relative_to(self.media_root)
            except ValueError as exc:
                raise ValueError(f"video material is outside data/inbox: {value}") from exc
            materials.append({"provider": "local", "url": relative.as_posix()})
        return materials

    def _custom_audio_payload(self, task: ContentTask) -> str | None:
        if not task.audio_path:
            return None
        candidate = Path(task.audio_path)
        if not candidate.is_absolute():
            candidate = self.media_root / candidate
        resolved = candidate.resolve(strict=True)
        try:
            relative = resolved.relative_to(self.media_root)
        except ValueError as exc:
            raise ValueError("custom audio is outside data/inbox") from exc
        return relative.as_posix()

    def create_video(self, task: ContentTask) -> str:
        payload = {
            "video_subject": task.topic,
            "video_script": task.script,
            "video_aspect": "9:16",
            "video_source": "local",
            "video_materials": self._material_payload(task),
            "video_count": 1,
            "subtitle_enabled": True,
            "voice_name": "zh-CN-XiaoxiaoNeural-Female",
        }
        custom_audio_file = self._custom_audio_payload(task)
        if custom_audio_file:
            payload["custom_audio_file"] = custom_audio_file
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/videos",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(json_content=True),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        data = result.get("data") or {}
        task_id = data.get("task_id")
        if not task_id:
            raise ValueError(f"MoneyPrinterTurbo did not return a task id: {result}")
        return task_id

    def get_video_status(self, task_id: str) -> VideoJobStatus:
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/tasks/{task_id}",
            headers=self._headers(),
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        data = result.get("data") or {}
        state = int(data.get("state", 4))
        progress = int(data.get("progress", 0))
        if state == -1:
            return VideoJobStatus(
                state=state,
                progress=progress,
                error=str(data.get("error") or data.get("failed_stage") or "video failed"),
            )
        if state != 1:
            return VideoJobStatus(state=state, progress=progress)

        videos = data.get("videos") or []
        if not videos:
            return VideoJobStatus(
                state=-1,
                progress=progress,
                error="video engine completed without a video file",
            )
        filename = Path(urlparse(str(videos[0])).path).name
        candidate = (self.output_root / task_id / filename).resolve()
        try:
            candidate.relative_to(self.output_root)
        except ValueError as exc:
            raise ValueError("video engine returned an unsafe output path") from exc
        if not candidate.is_file():
            return VideoJobStatus(state=state, progress=progress)
        return VideoJobStatus(state=state, progress=progress, media_path=candidate)
