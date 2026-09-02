from __future__ import annotations

import json
import random
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..models import AssetKind
from .base import (
    GenerationRequest,
    MediaGenerationError,
    MediaJob,
    MediaJobStatus,
    MediaRuntimeUnavailableError,
)


class ComfyUIProvider:
    name = "comfyui"

    def __init__(
        self,
        base_url: str,
        output_root: Path,
        image_workflow: Path,
        video_workflow: Path,
        *,
        timeout_seconds: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.output_root = output_root.resolve()
        self.workflows = {
            AssetKind.IMAGE: image_workflow,
            AssetKind.VIDEO: video_workflow,
        }
        self.timeout_seconds = timeout_seconds

    def available(self, kind: AssetKind) -> bool:
        if not self.workflows[kind].is_file():
            return False
        try:
            with urllib.request.urlopen(f"{self.base_url}/system_stats", timeout=3) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def submit(self, request: GenerationRequest) -> MediaJob:
        workflow_path = self.workflows[request.kind]
        if not workflow_path.is_file():
            raise MediaGenerationError(
                f"{request.kind.value} workflow is not configured: {workflow_path}"
            )
        template = json.loads(workflow_path.read_text(encoding="utf-8"))
        replacements = {
            "{{PROMPT}}": request.prompt,
            "{{NEGATIVE_PROMPT}}": request.negative_prompt,
            "{{WIDTH}}": request.width,
            "{{HEIGHT}}": request.height,
            "{{DURATION_SECONDS}}": request.duration_seconds,
            "{{FRAME_COUNT}}": max(17, request.duration_seconds * 16 + 1),
            "{{SEED}}": request.seed if request.seed >= 0 else random.randrange(2**31),
            "{{FILENAME_PREFIX}}": f"{request.task_id}/{request.shot_id}",
        }
        workflow = self._replace(template, replacements)
        payload = json.dumps({"prompt": workflow}, ensure_ascii=False).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.base_url}/prompt",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise MediaGenerationError(
                f"ComfyUI rejected workflow with HTTP {exc.code}: {detail}"
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise MediaRuntimeUnavailableError("ComfyUI is unreachable") from exc
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise MediaGenerationError(f"ComfyUI did not return prompt_id: {result}")
        return MediaJob(id=str(prompt_id), provider=self.name)

    def poll(self, job_id: str, kind: AssetKind) -> MediaJobStatus:
        try:
            with urllib.request.urlopen(
                f"{self.base_url}/history/{job_id}", timeout=self.timeout_seconds
            ) as response:
                history = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError) as exc:
            raise MediaRuntimeUnavailableError("ComfyUI history endpoint is unreachable") from exc
        record = history.get(job_id)
        if not record:
            if self._is_queued(job_id):
                return MediaJobStatus(state="processing")
            return MediaJobStatus(
                state="failed",
                error="ComfyUI job is missing from both queue and history",
            )
        status = record.get("status") or {}
        if status.get("status_str") == "error" or status.get("completed") is False:
            messages = status.get("messages") or []
            return MediaJobStatus(state="failed", error=self._format_error(messages))
        output = self._find_output(record.get("outputs") or {}, kind)
        if output is None:
            return MediaJobStatus(state="processing", progress=95)
        candidate = (self.output_root / output).resolve()
        try:
            candidate.relative_to(self.output_root)
        except ValueError as exc:
            raise MediaGenerationError("ComfyUI returned an unsafe output path") from exc
        if not candidate.is_file():
            return MediaJobStatus(state="processing", progress=95)
        return MediaJobStatus(state="complete", progress=100, media_path=candidate)

    def _is_queued(self, job_id: str) -> bool:
        try:
            with urllib.request.urlopen(
                f"{self.base_url}/queue", timeout=self.timeout_seconds
            ) as response:
                queue = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError) as exc:
            raise MediaRuntimeUnavailableError("ComfyUI queue endpoint is unreachable") from exc
        for key in ("queue_running", "queue_pending"):
            for item in queue.get(key) or []:
                if isinstance(item, list) and len(item) > 1 and str(item[1]) == job_id:
                    return True
        return False

    @staticmethod
    def _format_error(messages: list[Any]) -> str:
        """Keep the actionable ComfyUI exception instead of a tensor dump tail."""
        for message in reversed(messages):
            if not isinstance(message, (list, tuple)) or len(message) < 2:
                continue
            event, detail = message[0], message[1]
            if event != "execution_error" or not isinstance(detail, dict):
                continue
            node = detail.get("node_type") or "unknown node"
            node_id = detail.get("node_id") or "?"
            error_type = detail.get("exception_type") or "ExecutionError"
            error_message = str(detail.get("exception_message") or "generation failed").strip()
            return f"ComfyUI {node} node {node_id}: {error_type}: {error_message}"[:500]
        return "ComfyUI generation failed without an actionable error message"

    @classmethod
    def _replace(cls, value: Any, replacements: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: cls._replace(item, replacements) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._replace(item, replacements) for item in value]
        if isinstance(value, str):
            if value in replacements:
                return replacements[value]
            for token, replacement in replacements.items():
                value = value.replace(token, str(replacement))
        return value

    @staticmethod
    def _find_output(outputs: dict[str, Any], kind: AssetKind) -> Path | None:
        keys = ("gifs", "videos", "images") if kind == AssetKind.VIDEO else ("images",)
        for node in outputs.values():
            for key in keys:
                for item in node.get(key) or []:
                    filename = item.get("filename")
                    if not filename:
                        continue
                    subfolder = item.get("subfolder") or ""
                    return Path(subfolder) / filename
        return None
