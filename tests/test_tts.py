import json
from typing import ClassVar

import pytest

from open_media_flow.tts import LocalTTSClient, TTSError


class StubResponse:
    payload: ClassVar[dict] = {}
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_local_tts_resolves_output_under_inbox(tmp_path, monkeypatch):
    media_root = tmp_path / "inbox"
    audio = media_root / "generated/audio/task-1/narration.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    StubResponse.payload = {
        "relative_path": "generated/audio/task-1/narration.mp3"
    }
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["key"] = request.headers["X-api-key"]
        return StubResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = LocalTTSClient("http://host.docker.internal:8090", "secret", media_root)

    assert client.synthesize("task-1", "这是一段本地配音") == audio
    assert captured["body"] == {"task_id": "task-1", "text": "这是一段本地配音"}
    assert captured["key"] == "secret"


def test_local_tts_rejects_unsafe_runtime_path(tmp_path, monkeypatch):
    StubResponse.payload = {"relative_path": "../../outside.mp3"}
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: StubResponse())
    client = LocalTTSClient("http://127.0.0.1:8090", "", tmp_path / "inbox")

    with pytest.raises(TTSError, match="unsafe path"):
        client.synthesize("task-1", "这是一段本地配音")


def test_local_runtime_resolves_interpolated_video(tmp_path, monkeypatch):
    media_root = tmp_path / "inbox"
    video = media_root / "generated/interpolated/task-1/shot-1-2x.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    StubResponse.payload = {
        "relative_path": "generated/interpolated/task-1/shot-1-2x.mp4",
        "provider": "rife-mlx",
    }
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return StubResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = LocalTTSClient("http://127.0.0.1:8090", "", media_root)

    output, provider = client.interpolate(
        "task-1",
        "shot-1",
        str(media_root / "generated/comfyui/source.mp4"),
        multiplier=2,
    )

    assert output == video
    assert provider == "rife-mlx"
    assert captured["body"]["multiplier"] == 2
    assert captured["body"]["source_path"] == "generated/comfyui/source.mp4"
    assert captured["timeout"] == 1260
