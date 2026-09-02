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
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def synthesize(self, task_id: str, text: str) -> Path:
        request = urllib.request.Request(
            f"{self.base_url}/v1/audio/speech",
            data=json.dumps({"task_id": task_id, "text": text}, ensure_ascii=False).encode(),
            headers=self._headers(json_content=True),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
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

    def _headers(self, *, json_content: bool = False) -> dict[str, str]:
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers
