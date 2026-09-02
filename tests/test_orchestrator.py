import pytest

from open_media_flow.llm import LLMError
from open_media_flow.models import (
    Automation,
    AutomationCreate,
    AutomationRun,
    ContentTask,
    Platform,
    TaskStatus,
)
from open_media_flow.orchestrator import AutomationAlreadyRunningError, AutomationEngine


class MemoryAutomationStore:
    def __init__(self):
        self.automation = Automation(
            name="每日本地内容",
            topic="本地自动化内容",
            platforms=[Platform.BILIBILI, Platform.YOUTUBE],
            video_materials=["series/clip.mp4"],
        )
        self.tasks = {}
        self.runs = {}

    def get_automation(self, automation_id):
        assert automation_id == self.automation.id
        return self.automation

    def save_automation(self, automation):
        self.automation = automation
        return automation

    def create(self, task):
        self.tasks[task.id] = task
        return task

    def list(self):
        return list(self.tasks.values())

    def create_run(self, run):
        self.runs[run.id] = run
        return run

    def get(self, task_id):
        return self.tasks[task_id]

    def save(self, task):
        self.tasks[task.id] = task
        return task

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def save_run(self, run):
        self.runs[run.id] = run
        return run

    def delete_automation(self, automation_id):
        assert automation_id == self.automation.id
        self.automation = None


def test_manual_automation_run_creates_tracked_task():
    store = MemoryAutomationStore()
    engine = AutomationEngine.__new__(AutomationEngine)
    engine.store = store

    run = engine.create_task_from_automation(store.automation.id)

    assert isinstance(run, AutomationRun)
    task = store.tasks[run.task_id]
    assert task.automation_id == store.automation.id
    assert task.automation_run_id == run.id
    assert task.video_materials == ["series/clip.mp4"]
    assert task.events[0].stage == "策划"
    assert task.events[0].status == "draft"
    assert store.automation.last_run_at is not None

    with pytest.raises(AutomationAlreadyRunningError):
        engine.create_task_from_automation(store.automation.id)


def test_delete_automation_removes_schedule_and_record():
    class MemoryScheduler:
        def __init__(self, job_id):
            self.job_id = job_id
            self.removed = []

        def get_job(self, job_id):
            return object() if job_id == self.job_id else None

        def remove_job(self, job_id):
            self.removed.append(job_id)

    store = MemoryAutomationStore()
    job_id = f"automation:{store.automation.id}"
    engine = AutomationEngine.__new__(AutomationEngine)
    engine.store = store
    engine.scheduler = MemoryScheduler(job_id)

    engine.delete_automation(store.automation.id)

    assert engine.scheduler.removed == [job_id]
    assert store.automation is None


def test_update_automation_resyncs_schedule():
    class MemoryScheduler:
        def __init__(self):
            self.added = []

        def add_job(self, *args, **kwargs):
            self.added.append((args, kwargs))

    store = MemoryAutomationStore()
    engine = AutomationEngine.__new__(AutomationEngine)
    engine.store = store
    engine.scheduler = MemoryScheduler()

    updated = engine.update_automation(
        store.automation.id,
        AutomationCreate(
            name="每周内容复盘",
            topic="复盘本周本地 AI 工具",
            platforms=[Platform.BILIBILI],
            interval_minutes=10080,
            enabled=True,
        ),
    )

    assert updated.name == "每周内容复盘"
    assert updated.interval_minutes == 10080
    assert engine.scheduler.added[0][1]["id"] == f"automation:{updated.id}"


def test_cancel_task_marks_task_and_run_terminal():
    store = MemoryAutomationStore()
    engine = AutomationEngine.__new__(AutomationEngine)
    engine.store = store
    run = engine.create_task_from_automation(store.automation.id)

    task = engine.cancel_task(run.task_id)

    assert task.status == TaskStatus.CANCELLED
    assert task.events[-1].stage == "终止"
    assert task.events[-1].status == "cancelled"
    assert store.runs[run.id].status == "cancelled"


def test_failed_attempt_records_retry_state_and_specific_error():
    class FailingRouter:
        def generate_content_plan(self, _task):
            raise LLMError("primary returned invalid content package: 字段 script 太短")

    store = MemoryAutomationStore()
    task = ContentTask(topic="竖屏轻故事", platforms=[Platform.DOUYIN])
    store.create(task)
    engine = AutomationEngine.__new__(AutomationEngine)
    engine.store = store
    engine.llm_router = FailingRouter()
    engine.max_attempts = 3
    engine.tick_seconds = 15

    engine._advance(task)

    assert task.status == TaskStatus.DRAFT
    assert task.automation_attempts == 1
    assert task.metadata["automation_retry"] == {
        "stage": "draft",
        "attempt": 1,
        "max_attempts": 3,
        "will_retry": True,
        "next_retry_after_seconds": 15,
    }
    assert "字段 script" in task.automation_error
    assert "等待自动修复重试" in task.events[-1].detail


def test_unavailable_runtime_waits_without_consuming_attempts():
    from open_media_flow.models import ContentPlan

    class UnavailableProvider:
        def available(self, _kind):
            return False

    store = MemoryAutomationStore()
    task = ContentTask(topic="竖屏轻故事", platforms=[Platform.DOUYIN])
    task.status = TaskStatus.PLANNED
    task.content_plan = ContentPlan(
        audience="本地创作者",
        hook="一个意外的本地故事",
        creative_direction="电影感竖屏短片",
        cover_prompt="cinematic vertical cover, warm light",
        shots=[
            {
                "order": i,
                "narration": "这是一段完整旁白",
                "visual_prompt": "cinematic scene with warm natural light",
            }
            for i in range(1, 4)
        ],
    )
    run = AutomationRun(automation_id=store.automation.id, task_id=task.id)
    task.automation_run_id = run.id
    store.create(task)
    store.create_run(run)
    engine = AutomationEngine.__new__(AutomationEngine)
    engine.store = store
    engine.media_generation_enabled = True
    engine.media_provider = UnavailableProvider()

    engine._advance(task)

    assert task.status == TaskStatus.PLANNED
    assert task.automation_attempts == 0
    assert task.metadata["waiting_for_runtime"]["component"] == "video"
    assert store.runs[run.id].status == "waiting_for_media_runtime"


def test_history_timeout_waits_and_clears_misclassified_attempts():
    from open_media_flow.media_providers import MediaRuntimeUnavailableError
    from open_media_flow.models import AssetStatus, ContentPlan

    class BusyProvider:
        def available(self, _kind):
            return True

        def poll(self, _job_id, _kind):
            raise MediaRuntimeUnavailableError("ComfyUI history endpoint is unreachable")

    store = MemoryAutomationStore()
    task = ContentTask(topic="竖屏轻故事", platforms=[Platform.DOUYIN])
    task.status = TaskStatus.ASSETS_GENERATING
    task.automation_attempts = 2
    task.automation_error = "ComfyUI history endpoint is unreachable"
    task.metadata["automation_retry"] = {"stage": "assets_generating", "attempt": 2}
    task.content_plan = ContentPlan(
        audience="本地创作者",
        hook="一个意外的本地故事",
        creative_direction="电影感竖屏短片",
        cover_prompt="cinematic vertical cover, warm light",
        shots=[
            {
                "order": i,
                "narration": "这是一段完整旁白",
                "visual_prompt": "cinematic scene with warm natural light",
                "status": AssetStatus.QUEUED,
                "generation_job_id": f"job-{i}",
            }
            for i in range(1, 4)
        ],
    )
    run = AutomationRun(automation_id=store.automation.id, task_id=task.id)
    task.automation_run_id = run.id
    store.create(task)
    store.create_run(run)
    engine = AutomationEngine.__new__(AutomationEngine)
    engine.store = store
    engine.media_provider = BusyProvider()

    engine._advance(task)

    assert task.status == TaskStatus.ASSETS_GENERATING
    assert task.automation_attempts == 0
    assert task.automation_error is None
    assert "automation_retry" not in task.metadata
    assert task.metadata["waiting_for_runtime"]["component"] == "comfyui"
    assert store.runs[run.id].status == "waiting_for_media_runtime"


def test_retry_failed_asset_resumes_without_regenerating_content():
    from open_media_flow.models import AssetStatus, ContentPlan

    store = MemoryAutomationStore()
    task = ContentTask(topic="竖屏轻故事", platforms=[Platform.DOUYIN])
    task.status = TaskStatus.AUTOMATION_FAILED
    task.audio_path = "/tmp/narration.mp3"
    task.automation_attempts = 3
    task.automation_error = "ComfyUI failed"
    task.metadata["automation_retry"] = {"stage": "assets_generating"}
    task.content_plan = ContentPlan(
        audience="本地创作者",
        hook="一个意外的本地故事",
        creative_direction="电影感竖屏短片",
        cover_prompt="cinematic vertical cover, warm light",
        shots=[
            {
                "order": i,
                "narration": "这是一段完整旁白",
                "visual_prompt": "cinematic scene with warm natural light",
                "status": AssetStatus.FAILED,
            }
            for i in range(1, 4)
        ],
    )
    store.create(task)
    engine = AutomationEngine.__new__(AutomationEngine)
    engine.store = store

    resumed = engine.retry_task(task.id)

    assert resumed.status == TaskStatus.PLANNED
    assert resumed.automation_attempts == 0
    assert resumed.automation_error is None
    assert all(shot.status == AssetStatus.PENDING for shot in resumed.content_plan.shots)
