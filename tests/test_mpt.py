import json
from typing import ClassVar

import pytest

from open_media_flow.models import ContentTask, Platform
from open_media_flow.mpt import MoneyPrinterClient


class StubResponse:
    payload: ClassVar[dict] = {"data": {"task_id": "video-job-1"}}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_video_engine_uses_materials_from_embedded_data_root(tmp_path, monkeypatch):
    media_root = tmp_path / "inbox"
    material = media_root / "series" / "clip.mp4"
    material.parent.mkdir(parents=True)
    material.write_bytes(b"video")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return StubResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = MoneyPrinterClient("http://video-engine:8080", media_root, "secret")
    task = ContentTask(
        topic="本地视频",
        script="用于测试内置视频引擎的脚本内容。",
        platforms=[Platform.BILIBILI],
        video_materials=["series/clip.mp4"],
    )

    assert client.create_video(task) == "video-job-1"
    payload = json.loads(captured["request"].data)
    assert payload["video_materials"] == [
        {"provider": "local", "url": "series/clip.mp4"}
    ]
    assert captured["request"].headers["X-api-key"] == "secret"


def test_video_engine_passes_generated_audio_from_inbox(tmp_path, monkeypatch):
    media_root = tmp_path / "inbox"
    material = media_root / "generated" / "video" / "shot.mp4"
    audio = media_root / "generated" / "audio" / "task-1" / "narration.mp3"
    material.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    material.write_bytes(b"video")
    audio.write_bytes(b"audio")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return StubResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = MoneyPrinterClient("http://video-engine:8080", media_root)
    task = ContentTask(
        topic="本地配音",
        script="由宿主机原生语音运行时生成的旁白。",
        platforms=[Platform.DOUYIN],
        video_materials=[str(material)],
        audio_path=str(audio),
    )

    assert client.create_video(task) == "video-job-1"
    assert captured["payload"]["custom_audio_file"] == (
        "generated/audio/task-1/narration.mp3"
    )


def test_video_engine_rejects_custom_audio_outside_inbox(tmp_path):
    media_root = tmp_path / "inbox"
    material = media_root / "clip.mp4"
    media_root.mkdir()
    material.write_bytes(b"video")
    outside = tmp_path / "narration.mp3"
    outside.write_bytes(b"audio")
    client = MoneyPrinterClient("http://video-engine:8080", media_root)
    task = ContentTask(
        topic="音频越界",
        script="用于验证自定义音频目录安全边界的测试脚本。",
        platforms=[Platform.YOUTUBE],
        video_materials=[str(material)],
        audio_path=str(outside),
    )

    with pytest.raises(ValueError, match="custom audio is outside data/inbox"):
        client.create_video(task)


def test_video_engine_rejects_material_outside_inbox(tmp_path):
    media_root = tmp_path / "inbox"
    media_root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    client = MoneyPrinterClient("http://video-engine:8080", media_root)
    task = ContentTask(
        topic="越界素材",
        script="用于确认素材目录边界的测试脚本内容。",
        platforms=[Platform.YOUTUBE],
        video_materials=[str(outside)],
    )

    with pytest.raises(ValueError, match="outside data/inbox"):
        client.create_video(task)


def test_video_status_resolves_completed_file(tmp_path, monkeypatch):
    class CompletedResponse(StubResponse):
        payload: ClassVar[dict] = {
            "data": {
                "task_id": "video-job-1",
                "state": 1,
                "progress": 100,
                "videos": ["/tasks/video-job-1/final-1.mp4"],
            }
        }

    output = tmp_path / "output" / "video-job-1"
    output.mkdir(parents=True)
    video = output / "final-1.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: CompletedResponse())
    client = MoneyPrinterClient(
        "http://video-engine:8080",
        tmp_path / "inbox",
        output_root=tmp_path / "output",
    )

    status = client.get_video_status("video-job-1")

    assert status.state == 1
    assert status.progress == 100
    assert status.media_path == video
