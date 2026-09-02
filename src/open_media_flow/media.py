from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class UnsafeMediaPathError(ValueError):
    pass


def resolve_media_path(value: str, allowed_roots: tuple[Path, ...]) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if not any(path.is_relative_to(root) for root in allowed_roots):
        raise UnsafeMediaPathError(f"media must be inside an allowed root: {path}")
    return path


def probe_media(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height,duration",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or "ffprobe failed")
    return json.loads(completed.stdout)

