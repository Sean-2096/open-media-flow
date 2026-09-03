from dataclasses import replace

from fastapi.testclient import TestClient

from open_media_flow import api
from open_media_flow.llm import GeneratedMetadata, LLMGeneration
from open_media_flow.models import (
    AuditCheck,
    AuditReport,
    Automation,
    ContentTask,
    Platform,
    PublishResult,
    TaskStatus,
)
from open_media_flow.mpt import VideoJobStatus
from open_media_flow.store import JsonTaskStore


def test_api_requires_key_and_creates_task(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "store", JsonTaskStore(tmp_path / "tasks.json"))
    client = TestClient(api.app)

    unauthorized = client.post(
        "/tasks",
        json={"topic": "本地工作流", "platforms": ["bilibili"]},
    )
    assert unauthorized.status_code == 401

    created = client.post(
        "/tasks",
        headers={"X-API-Key": "change-me"},
        json={"topic": "本地工作流", "platforms": ["bilibili"]},
    )
    assert created.status_code == 200
    assert created.json()["status"] == "draft"


def test_dashboard_is_served_without_account_login():
    client = TestClient(api.app)

    response = client.get("/")
    stylesheet = client.get("/assets/app.css")
    script = client.get("/assets/app.js")
    dashboard_tasks = client.get(
        "/tasks",
        headers={"Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 200
    assert "本地内容调度台" in response.text
    assert stylesheet.status_code == 200
    assert "--signal" in stylesheet.text
    assert "DELETE AUTOMATION" in response.text
    assert script.status_code == 200
    assert "localStorage.setItem(apiKeyStorageKey" in script.text
    assert "automation-progress" in script.text
    assert "taskProgress" in script.text
    assert "EVENT STREAM" in script.text
    assert "MEDIA ASSETS" in script.text
    assert "FINAL CUT READY" in script.text
    assert "SHOT TIMELINE" in script.text
    assert "data-preview-seek" in script.text
    assert "detailModal.dataset.taskVersion" in script.text
    assert "DEBUG CONTEXT" in script.text
    assert "clear-api-key-button" in response.text
    assert dashboard_tasks.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "omf_dashboard_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "change-me" not in cookie


def test_task_preview_streams_local_media_with_range_support(tmp_path, monkeypatch):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    video = allowed_root / "final.mp4"
    video.write_bytes(b"0123456789")
    cover = allowed_root / "cover.png"
    cover.write_bytes(b"fake-png")
    task_store = JsonTaskStore(tmp_path / "tasks.json")
    task = task_store.create(
        ContentTask(
            topic="本地成片预览",
            platforms=["bilibili"],
            media_path=str(video),
            cover_path=str(cover),
        )
    )
    monkeypatch.setattr(api, "store", task_store)
    monkeypatch.setattr(
        api,
        "settings",
        replace(api.settings, allowed_media_roots=(allowed_root.resolve(),)),
    )
    client = TestClient(api.app)
    headers = {"X-API-Key": "change-me"}

    preview = client.get(
        f"/tasks/{task.id}/preview",
        headers={**headers, "Range": "bytes=2-5"},
    )
    cover_response = client.get(f"/tasks/{task.id}/cover", headers=headers)

    assert preview.status_code == 206
    assert preview.content == b"2345"
    assert preview.headers["content-range"] == "bytes 2-5/10"
    assert preview.headers["content-type"] == "video/mp4"
    assert cover_response.status_code == 200
    assert cover_response.headers["content-type"] == "image/png"


def test_task_preview_rejects_media_outside_allowed_roots(tmp_path, monkeypatch):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    video = tmp_path / "outside.mp4"
    video.write_bytes(b"outside")
    task_store = JsonTaskStore(tmp_path / "tasks.json")
    task = task_store.create(
        ContentTask(
            topic="越界媒体",
            platforms=["youtube"],
            media_path=str(video),
        )
    )
    monkeypatch.setattr(api, "store", task_store)
    monkeypatch.setattr(
        api,
        "settings",
        replace(api.settings, allowed_media_roots=(allowed_root.resolve(),)),
    )
    client = TestClient(api.app)

    response = client.get(
        f"/tasks/{task.id}/preview",
        headers={"X-API-Key": "change-me"},
    )

    assert response.status_code == 403


def test_read_task_refreshes_manual_video_status(tmp_path, monkeypatch):
    output = tmp_path / "output" / "video-job-1" / "final-1.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")
    task_store = JsonTaskStore(tmp_path / "tasks.json")
    task = task_store.create(
        ContentTask(
            topic="手动视频任务",
            platforms=[Platform.BILIBILI],
            generation_job_id="video-job-1",
            status=TaskStatus.GENERATED,
        )
    )

    class StubMPT:
        def get_video_status(self, task_id):
            assert task_id == "video-job-1"
            return VideoJobStatus(state=1, progress=100, media_path=output)

    monkeypatch.setattr(api, "store", task_store)
    monkeypatch.setattr(api, "mpt", StubMPT())
    client = TestClient(api.app)

    response = client.get(
        f"/tasks/{task.id}",
        headers={"X-API-Key": "change-me"},
    )

    assert response.status_code == 200
    assert response.json()["media_path"] == str(output)
    assert response.json()["metadata"]["video_progress"] == 100
    assert task_store.get(task.id).media_path == str(output)


def test_publish_rejects_stale_approved_audit(tmp_path, monkeypatch):
    task_store = JsonTaskStore(tmp_path / "tasks.json")
    task = task_store.create(
        ContentTask(
            topic="已修改的内容",
            platforms=[Platform.YOUTUBE],
            status=TaskStatus.DRAFT,
            audit=AuditReport(
                approved=True,
                score=100,
                checks=[AuditCheck(name="media", passed=True, score=100, detail="ok")],
            ),
        )
    )
    monkeypatch.setattr(api, "store", task_store)
    client = TestClient(api.app)

    response = client.post(
        f"/tasks/{task.id}/publish",
        headers={"X-API-Key": "change-me"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "task must pass review before publishing"


def test_dashboard_can_update_automation_and_cancel_task(monkeypatch):
    class StubAutomationEngine:
        def update_automation(self, automation_id, body):
            return Automation(id=automation_id, **body.model_dump())

        def cancel_task(self, task_id):
            return ContentTask(
                id=task_id,
                topic="停止中的本地任务",
                platforms=["bilibili"],
                status=TaskStatus.CANCELLED,
            )

    monkeypatch.setattr(api, "automation_engine", StubAutomationEngine())
    client = TestClient(api.app)
    headers = {"X-API-Key": "change-me"}

    updated = client.put(
        "/automations/example-plan",
        headers=headers,
        json={
            "name": "更新后的计划",
            "topic": "更新后的内容主题",
            "platforms": ["bilibili"],
            "interval_minutes": 1440,
            "enabled": False,
        },
    )
    cancelled = client.post("/tasks/example-task/cancel", headers=headers)

    assert updated.status_code == 200
    assert updated.json()["name"] == "更新后的计划"
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_delete_automation_endpoint_removes_schedule_and_record(monkeypatch):
    class StubAutomationEngine:
        def __init__(self):
            self.deleted = []

        def delete_automation(self, automation_id):
            self.deleted.append(automation_id)

    engine = StubAutomationEngine()
    monkeypatch.setattr(api, "automation_engine", engine)
    client = TestClient(api.app)

    response = client.delete(
        "/automations/example-plan",
        headers={"X-API-Key": "change-me"},
    )

    assert response.status_code == 204
    assert engine.deleted == ["example-plan"]


def test_generate_metadata_records_selected_endpoint(tmp_path, monkeypatch):
    class StubRouter:
        def generate_metadata(self, task):
            return LLMGeneration(
                metadata=GeneratedMetadata(
                    title="统一模型路由",
                    script=(
                        "这是一段用于确认统一模型路由的测试脚本。系统会先读取任务主题和目标平台，"
                        "再调用本地主模型生成标题、正文、简介与标签。生成结果必须通过结构化校验，"
                        "包括标题长度、脚本长度、标签数量以及人工智能辅助生成标识。随后接口会把"
                        "实际使用的端点名称和模型标识写入任务元数据，方便后续审核、排障和成本统计。"
                        "如果本地主模型请求失败，路由器会按照配置执行有限次数的重试；只有用户明确"
                        "启用云端回退并配置密钥时，系统才会调用备用端点。测试环境默认保持模拟发布，"
                        "不会访问或修改任何真实平台账号。内容生成完成后，还要经过规则审核、模型审核、"
                        "媒体文件检查和发布状态检查，确保每一步都有清晰的输入、输出和失败原因。"
                        "这套流程让开发者能够在本地重复验证核心链路，同时避免泄露密钥或误发内容。"
                        "最终测试会断言任务中记录的端点和模型与路由器返回结果一致，从而证明 API"
                        "正确保存了本次生成所使用的来源，并为后续自动化编排提供可靠依据。"
                    ),
                    description="测试简介。本内容包含AI辅助生成素材",
                    tags=["本地AI", "路由", "自动化"],
                ),
                endpoint="fallback",
                model="cloud-review-model",
            )

    monkeypatch.setattr(api, "store", JsonTaskStore(tmp_path / "tasks.json"))
    monkeypatch.setattr(api, "llm_router", StubRouter())
    client = TestClient(api.app)
    headers = {"X-API-Key": "change-me"}
    created = client.post(
        "/tasks",
        headers=headers,
        json={"topic": "统一模型路由", "platforms": ["youtube"]},
    ).json()
    reviewed = api.store.get(created["id"])
    reviewed.status = TaskStatus.APPROVED
    reviewed.audit = AuditReport(
        approved=True,
        score=100,
        checks=[AuditCheck(name="media", passed=True, score=100, detail="ok")],
    )
    reviewed.publish_results = [
        PublishResult(
            platform=Platform.YOUTUBE,
            success=True,
            detail="previous publish",
        )
    ]
    api.store.save(reviewed)

    generated = client.post(
        f"/tasks/{created['id']}/generate-metadata",
        headers=headers,
    )

    assert generated.status_code == 200
    assert generated.json()["metadata"]["llm_generation"] == {
        "endpoint": "fallback",
        "model": "cloud-review-model",
    }
    assert generated.json()["status"] == "draft"
    assert generated.json()["audit"] is None
    assert generated.json()["publish_results"] == []
