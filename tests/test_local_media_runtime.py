from fastapi.testclient import TestClient

from open_media_flow import local_media_runtime as runtime


class DormantThread:
    def __init__(self, *, target, args, **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        return None


def test_lip_sync_job_submission_and_polling(tmp_path, monkeypatch):
    video = tmp_path / "generated/comfyui/shot.mp4"
    audio = tmp_path / "generated/audio/shot.mp3"
    video.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")

    monkeypatch.setattr(runtime, "media_root", tmp_path)
    monkeypatch.setattr(runtime, "lip_sync_root", tmp_path / "generated/lip-sync")
    monkeypatch.setattr(runtime, "_musetalk_ready", lambda: True)
    monkeypatch.setattr(runtime, "_latentsync_ready", lambda: False)
    monkeypatch.setattr(runtime.threading, "Thread", DormantThread)
    runtime._lip_jobs.clear()

    client = TestClient(runtime.app)
    response = client.post(
        "/v1/video/lip-sync",
        json={
            "task_id": "task-1234",
            "shot_id": "shot-1",
            "video_path": "generated/comfyui/shot.mp4",
            "audio_path": "generated/audio/shot.mp3",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "musetalk-v1.5-mps"

    status = client.get(f"/v1/video/lip-sync/{payload['job_id']}")
    assert status.status_code == 200
    assert status.json()["state"] == "processing"
    assert status.json()["stage"] == "queued"
    assert status.json()["elapsed_seconds"] >= 0
    assert "started_at" not in status.json()


def test_lip_sync_rejects_when_engine_is_unavailable(monkeypatch):
    monkeypatch.setattr(runtime, "_musetalk_ready", lambda: False)
    monkeypatch.setattr(runtime, "_latentsync_ready", lambda: False)
    response = TestClient(runtime.app).post(
        "/v1/video/lip-sync",
        json={
            "task_id": "task-1234",
            "shot_id": "shot-1",
            "video_path": "generated/comfyui/shot.mp4",
            "audio_path": "generated/audio/shot.mp3",
        },
    )
    assert response.status_code == 503


def test_auto_lip_sync_prefers_latentsync_when_available(tmp_path, monkeypatch):
    video = tmp_path / "generated/comfyui/shot.mp4"
    audio = tmp_path / "generated/audio/shot.mp3"
    video.parent.mkdir(parents=True)
    audio.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    monkeypatch.setattr(runtime, "media_root", tmp_path)
    monkeypatch.setattr(runtime, "lip_sync_root", tmp_path / "generated/lip-sync")
    monkeypatch.setattr(runtime, "_latentsync_ready", lambda: True)
    monkeypatch.setattr(runtime, "_musetalk_ready", lambda: True)
    monkeypatch.setattr(runtime, "latentsync_base_url", "http://gpu-worker:8092")
    monkeypatch.setattr(runtime.threading, "Thread", DormantThread)
    runtime._lip_jobs.clear()

    response = TestClient(runtime.app).post(
        "/v1/video/lip-sync",
        json={
            "task_id": "task-1234",
            "shot_id": "shot-1",
            "video_path": "generated/comfyui/shot.mp4",
            "audio_path": "generated/audio/shot.mp3",
            "mode": "auto",
        },
    )

    assert response.status_code == 200
    assert response.json()["provider"] == "latentsync-v1.6-512"


def test_quality_mode_does_not_silently_use_musetalk(monkeypatch):
    monkeypatch.setattr(runtime, "_latentsync_ready", lambda: False)
    monkeypatch.setattr(runtime, "_musetalk_ready", lambda: True)
    response = TestClient(runtime.app).post(
        "/v1/video/lip-sync",
        json={
            "task_id": "task-1234",
            "shot_id": "shot-1",
            "video_path": "generated/comfyui/shot.mp4",
            "audio_path": "generated/audio/shot.mp3",
            "mode": "quality",
        },
    )
    assert response.status_code == 503
    assert "LatentSync" in response.json()["detail"]


def test_comic_shot_renderer_creates_48fps_motion_clip(tmp_path, monkeypatch):
    image = tmp_path / "generated/comfyui/panel.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    monkeypatch.setattr(runtime, "media_root", tmp_path.resolve())
    monkeypatch.setattr(
        runtime, "comic_root", (tmp_path / "generated/comic-motion").resolve()
    )
    monkeypatch.setattr(runtime, "_ffmpeg_executable", lambda: "/fake/ffmpeg")

    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        output = runtime.comic_root / "task-1234/shot-1-push_in.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    response = TestClient(runtime.app).post(
        "/v1/video/comic-shot",
        json={
            "task_id": "task-1234",
            "shot_id": "shot-1",
            "image_path": "generated/comfyui/panel.png",
            "duration_seconds": 5,
            "motion": "push_in",
        },
    )

    assert response.status_code == 200
    assert response.json()["provider"] == "comic-motion-ffmpeg"
    assert response.json()["fps"] == 48
    assert "zoompan" in captured["command"][captured["command"].index("-vf") + 1]
