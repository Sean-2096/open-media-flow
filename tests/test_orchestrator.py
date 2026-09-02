import pytest

from open_media_flow.models import Automation, AutomationRun, Platform
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
