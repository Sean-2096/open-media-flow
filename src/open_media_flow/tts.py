from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path


class TTSError(RuntimeError):
    pass


class LocalTTSClient:
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
                return response.status == 200 and result.get("tts_ready", True) is not False
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return False

    def interpolation_available(self) -> bool:
        request = urllib.request.Request(
            f"{self.base_url}/health", headers=self._headers()
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                result = json.loads(response.read().decode("utf-8"))
                return (
                    response.status == 200
                    and result.get("frame_interpolation_ready", False) is True
                )
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return False

    def synthesize(self, task_id: str, text: str, *, clip_id: str | None = None) -> Path:
        payload = {"task_id": task_id, "text": text}
        if clip_id:
            payload["clip_id"] = clip_id
        request = urllib.request.Request(
            f"{self.base_url}/v1/audio/speech",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers=self._headers(json_content=True),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TTSError(f"local TTS returned HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise TTSError("local TTS runtime is unreachable") from exc
        relative = Path(str(result.get("relative_path") or ""))
        candidate = (self.media_root / relative).resolve()
        try:
            candidate.relative_to(self.media_root)
        except ValueError as exc:
            raise TTSError("local TTS returned an unsafe path") from exc
        if not candidate.is_file():
            raise TTSError("local TTS output file is missing")
        return candidate

    def interpolate(
        self,
        task_id: str,
        shot_id: str,
        source_path: str,
        *,
        multiplier: int = 2,
    ) -> tuple[Path, str]:
        source = Path(source_path)
        request_source = source_path
        if source.is_absolute():
            try:
                request_source = source.resolve().relative_to(self.media_root).as_posix()
            except ValueError:
                request_source = source_path
        request = urllib.request.Request(
            f"{self.base_url}/v1/video/interpolate",
            data=json.dumps(
                {
                    "task_id": task_id,
                    "shot_id": shot_id,
                    "source_path": request_source,
                    "multiplier": multiplier,
                }
            ).encode("utf-8"),
            headers=self._headers(json_content=True),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=1_260) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail")
            except (ValueError, UnicodeDecodeError):
                detail = None
            raise TTSError(detail or f"frame interpolation returned HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise TTSError("local frame interpolation runtime is unreachable") from exc
        relative = Path(str(result.get("relative_path") or ""))
        candidate = (self.media_root / relative).resolve()
        try:
            candidate.relative_to(self.media_root)
        except ValueError as exc:
            raise TTSError("frame interpolation returned an unsafe path") from exc
        if not candidate.is_file():
            raise TTSError("frame interpolation output file is missing")
        return candidate, str(result.get("provider") or "rife-mlx")

    def _headers(self, *, json_content: bool = False) -> dict[str, str]:
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers
