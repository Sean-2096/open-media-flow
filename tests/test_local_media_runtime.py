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
