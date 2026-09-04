import json

from open_media_flow.media_providers import ComfyUIProvider, GenerationRequest
from open_media_flow.models import AssetKind


class StubResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return b'{"prompt_id":"comic-job-1"}'


def test_comic_generation_uses_dedicated_workflow(tmp_path, monkeypatch):
    regular = tmp_path / "image.json"
    comic = tmp_path / "comic.json"
    video = tmp_path / "video.json"
    regular.write_text('{"1":{"inputs":{"text":"regular"}}}')
    comic.write_text('{"1":{"inputs":{"text":"comic {{PROMPT}}"}}}')
    video.write_text("{}")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return StubResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = ComfyUIProvider(
        "http://127.0.0.1:8188",
        tmp_path,
        regular,
        video,
        comic_image_workflow=comic,
    )

    job = provider.submit(
        GenerationRequest(
            task_id="task-1",
            shot_id="panel-1",
            kind=AssetKind.IMAGE,
            prompt="anime hero",
            workflow_variant="comic",
        )
    )

    assert job.id == "comic-job-1"
    assert captured["payload"]["prompt"]["1"]["inputs"]["text"] == (
        "comic anime hero"
    )
