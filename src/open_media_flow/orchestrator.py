from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from redis import Redis

from .llm import FallbackLLMRouter
from .media_providers import ComfyUIProvider, GenerationRequest
from .models import (
    AssetKind,
    AssetStatus,
    Automation,
    AutomationCreate,
    AutomationRun,
    ContentTask,
    TaskStatus,
)
from .mpt import MoneyPrinterClient
from .pipeline import Pipeline
from .store import PostgresStore
from .tts import LocalTTSClient

logger = logging.getLogger(__name__)
_active_engine: AutomationEngine | None = None


class AutomationAlreadyRunningError(RuntimeError):
    pass


def execute_automation_job(automation_id: str) -> None:
    if _active_engine is not None:
        try:
            _active_engine.create_task_from_automation(automation_id)
        except AutomationAlreadyRunningError:
            logger.info("skip overlapping automation run: automation_id=%s", automation_id)


def process_automation_tasks_job() -> None:
    if _active_engine is not None:
        _active_engine.process_pending_tasks()


class AutomationEngine:
    def __init__(
        self,
        store: PostgresStore,
        llm_router: FallbackLLMRouter,
        mpt: MoneyPrinterClient,
        pipeline: Pipeline,
        media_provider: ComfyUIProvider,
        tts: LocalTTSClient,
        *,
        database_url: str,
        redis_url: str,
        timezone: str,
        tick_seconds: int,
        max_attempts: int,
        media_generation_enabled: bool,
    ):
        self.store = store
        self.llm_router = llm_router
        self.mpt = mpt
        self.pipeline = pipeline
        self.media_provider = media_provider
        self.tts = tts
        self.tick_seconds = tick_seconds
        self.max_attempts = max_attempts
        self.media_generation_enabled = media_generation_enabled
        self.redis = Redis.from_url(redis_url, decode_responses=True)
        self.scheduler = BackgroundScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=database_url)},
            timezone=timezone,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": max(30, tick_seconds * 2),
            },
        )

    @property
    def running(self) -> bool:
        return self.scheduler.running

    def start(self) -> None:
        global _active_engine
        _active_engine = self
        self.redis.ping()
        self.scheduler.start()
        self.scheduler.add_job(
            process_automation_tasks_job,
            "interval",
            seconds=self.tick_seconds,
            id="system:automation-processor",
            replace_existing=True,
        )
        for automation in self.store.list_automations():
            self.sync_schedule(automation)

    def stop(self) -> None:
        global _active_engine
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        _active_engine = None

    def create_automation(self, body: AutomationCreate) -> Automation:
        automation = self.store.create_automation(Automation(**body.model_dump()))
        self.sync_schedule(automation)
        return automation

    def sync_schedule(self, automation: Automation) -> None:
        job_id = f"automation:{automation.id}"
        if not automation.enabled:
            existing = self.scheduler.get_job(job_id)
            if existing is not None:
                self.scheduler.remove_job(job_id)
            return
        self.scheduler.add_job(
            execute_automation_job,
            "interval",
            minutes=automation.interval_minutes,
            args=[automation.id],
            id=job_id,
            replace_existing=True,
        )

    def set_enabled(self, automation_id: str, enabled: bool) -> Automation:
        automation = self.store.get_automation(automation_id)
        automation.enabled = enabled
        automation = self.store.save_automation(automation)
        self.sync_schedule(automation)
        return automation

    def delete_automation(self, automation_id: str) -> None:
        self.store.get_automation(automation_id)
        job_id = f"automation:{automation_id}"
        if self.scheduler.get_job(job_id) is not None:
            self.scheduler.remove_job(job_id)
        self.store.delete_automation(automation_id)

    def create_task_from_automation(self, automation_id: str) -> AutomationRun:
        automation = self.store.get_automation(automation_id)
        terminal = {
            TaskStatus.PUBLISHED,
            TaskStatus.REVIEW_REJECTED,
            TaskStatus.PARTIAL_FAILURE,
            TaskStatus.AUTOMATION_FAILED,
        }
        if any(
            task.automation_id == automation_id and task.status not in terminal
            for task in self.store.list()
        ):
            raise AutomationAlreadyRunningError(automation_id)
        task = ContentTask(
            topic=automation.topic,
            platforms=automation.platforms,
            video_materials=automation.video_materials,
            automation_id=automation.id,
        )
        run = AutomationRun(automation_id=automation.id, task_id=task.id)
        task.automation_run_id = run.id
        self.store.create(task)
        self.store.create_run(run)
        automation.last_run_at = datetime.now(UTC)
        self.store.save_automation(automation)
        return run

    def process_pending_tasks(self) -> None:
        lock = self.redis.lock(
            "open-media-flow:automation-processor",
            timeout=max(300, self.tick_seconds * 4),
            blocking=False,
        )
        if not lock.acquire(blocking=False):
            return
        try:
            terminal = {
                TaskStatus.PUBLISHED,
                TaskStatus.REVIEW_REJECTED,
                TaskStatus.PARTIAL_FAILURE,
                TaskStatus.AUTOMATION_FAILED,
            }
            for task in self.store.list():
                if task.automation_id and task.status not in terminal:
                    self._advance(task)
        finally:
            if lock.owned():
                lock.release()

    def _advance(self, task: ContentTask) -> None:
        try:
            if task.status == TaskStatus.DRAFT:
                generation = self.llm_router.generate_content_plan(task)
                task.title = generation.metadata.title
                task.script = generation.metadata.script
                task.description = generation.metadata.description
                task.tags = generation.metadata.tags
                task.content_plan = generation.plan
                task.metadata["llm_generation"] = {
                    "endpoint": generation.endpoint,
                    "model": generation.model,
                }
                task.status = TaskStatus.PLANNED
                task.automation_error = None
                self.store.save(task)

            if task.status == TaskStatus.PLANNED:
                if not self.media_generation_enabled:
                    self._set_run_status(
                        task,
                        "waiting_for_media_runtime",
                        "内容包和分镜已生成，等待启用本地媒体生成运行时",
                    )
                    return
                if task.content_plan is None:
                    raise ValueError("content plan is missing")
                if not self.media_provider.available(AssetKind.VIDEO):
                    raise ValueError("local video generation provider is unavailable")
                if not self.media_provider.available(AssetKind.IMAGE):
                    raise ValueError("local image generation provider is unavailable")
                if not task.audio_path:
                    if not self.tts.available():
                        raise ValueError("local speech generation runtime is unavailable")
                    task.audio_path = str(self.tts.synthesize(task.id, task.script))
                    self.store.save(task)
                for shot in task.content_plan.shots:
                    if shot.status != AssetStatus.PENDING:
                        continue
                    job = self.media_provider.submit(
                        GenerationRequest(
                            task_id=task.id,
                            shot_id=shot.id,
                            kind=AssetKind.VIDEO,
                            prompt=shot.visual_prompt,
                            negative_prompt=shot.negative_prompt,
                            duration_seconds=shot.duration_seconds,
                        )
                    )
                    shot.provider = job.provider
                    shot.generation_job_id = job.id
                    shot.status = AssetStatus.QUEUED
                    self.store.save(task)
                if not task.metadata.get("cover_generation_job_id"):
                    cover_job = self.media_provider.submit(
                        GenerationRequest(
                            task_id=task.id,
                            shot_id="cover",
                            kind=AssetKind.IMAGE,
                            prompt=task.content_plan.cover_prompt,
                        )
                    )
                    task.metadata["cover_generation_job_id"] = cover_job.id
                    task.metadata["cover_status"] = AssetStatus.QUEUED.value
                    self.store.save(task)
                task.status = TaskStatus.ASSETS_GENERATING
                self.store.save(task)

            if task.status == TaskStatus.ASSETS_GENERATING:
                if task.content_plan is None:
                    raise ValueError("content plan is missing")
                for shot in task.content_plan.shots:
                    if shot.status != AssetStatus.QUEUED or not shot.generation_job_id:
                        continue
                    result = self.media_provider.poll(
                        shot.generation_job_id, AssetKind.VIDEO
                    )
                    if result.error or result.state == "failed":
                        shot.status = AssetStatus.FAILED
                        shot.error = result.error or "video generation failed"
                        raise ValueError(f"shot {shot.order} failed: {shot.error}")
                    if result.media_path is not None:
                        shot.status = AssetStatus.COMPLETE
                        shot.media_path = str(result.media_path)
                cover_job_id = str(task.metadata.get("cover_generation_job_id") or "")
                if cover_job_id and task.metadata.get("cover_status") != "complete":
                    cover = self.media_provider.poll(cover_job_id, AssetKind.IMAGE)
                    if cover.error or cover.state == "failed":
                        raise ValueError(cover.error or "cover generation failed")
                    if cover.media_path is not None:
                        task.cover_path = str(cover.media_path)
                        task.metadata["cover_status"] = "complete"
                shots_complete = all(
                    shot.status == AssetStatus.COMPLETE
                    for shot in task.content_plan.shots
                )
                cover_complete = task.metadata.get("cover_status") == "complete"
                if not shots_complete or not cover_complete:
                    self.store.save(task)
                    return
                task.video_materials = [
                    shot.media_path
                    for shot in task.content_plan.shots
                    if shot.media_path is not None
                ]
                task.status = TaskStatus.COMPOSING
                self.store.save(task)

            if task.status == TaskStatus.COMPOSING:
                task.generation_job_id = self.mpt.create_video(task)
                task.status = TaskStatus.GENERATED
                self.store.save(task)

            if task.status == TaskStatus.GENERATED and not task.media_path:
                if not task.generation_job_id:
                    raise ValueError("video generation job id is missing")
                video = self.mpt.get_video_status(task.generation_job_id)
                task.metadata["video_progress"] = video.progress
                if video.error:
                    raise ValueError(video.error)
                if video.media_path is None:
                    self.store.save(task)
                    return
                task.media_path = str(video.media_path)
                self.store.save(task)

            if task.status == TaskStatus.GENERATED and task.media_path:
                task = self.pipeline.audit(task)
                if task.status == TaskStatus.REVIEW_REJECTED:
                    self._finish_run(task, "review_rejected", "内容未通过自动审核")
                    return

            if task.status == TaskStatus.APPROVED:
                task = self.pipeline.publish(task)
                status = "published" if task.status == TaskStatus.PUBLISHED else "failed"
                self._finish_run(task, status, "自动流程执行完成")
        except Exception as exc:
            task.automation_attempts += 1
            task.automation_error = str(exc)[:500]
            if task.automation_attempts >= self.max_attempts:
                task.status = TaskStatus.AUTOMATION_FAILED
                self._finish_run(task, "failed", task.automation_error)
            else:
                self.store.save(task)
            logger.exception("automation task failed: task_id=%s", task.id)

    def _finish_run(self, task: ContentTask, status: str, detail: str) -> None:
        self.store.save(task)
        self._set_run_status(task, status, detail)

    def _set_run_status(self, task: ContentTask, status: str, detail: str) -> None:
        if not task.automation_run_id:
            return
        run = self.store.get_run(task.automation_run_id)
        if run is None:
            return
        run.status = status
        run.detail = detail
        self.store.save_run(run)
