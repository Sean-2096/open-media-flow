from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from redis import Redis

from .llm import FallbackLLMRouter
from .media_providers import (
    ComfyUIProvider,
    GenerationRequest,
    MediaRuntimeUnavailableError,
)
from .models import (
    AssetKind,
    AssetStatus,
    Automation,
    AutomationCreate,
    AutomationRun,
    ContentTask,
    TaskEvent,
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

    def update_automation(self, automation_id: str, body: AutomationCreate) -> Automation:
        automation = self.store.get_automation(automation_id)
        for field, value in body.model_dump().items():
            setattr(automation, field, value)
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
            TaskStatus.CANCELLED,
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
        self._record_event(task, "策划", TaskStatus.DRAFT, "任务已创建，等待本地编排器生成内容方案")
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
                TaskStatus.CANCELLED,
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
                self._complete_stage(task)
                self._record_event(
                    task,
                    "策划",
                    task.status,
                    f"内容方案已生成，共 {len(task.content_plan.shots)} 个分镜",
                )
                self.store.save(task)
                self._set_run_status(task, "running", "内容方案已生成，正在准备媒体素材")

            if task.status == TaskStatus.PLANNED:
                if not self.media_generation_enabled:
                    self._wait_for_runtime(
                        task,
                        "media_generation",
                        "内容包和分镜已生成，等待启用本地媒体生成运行时",
                    )
                    return
                if task.content_plan is None:
                    raise ValueError("content plan is missing")
                if not self.media_provider.available(AssetKind.VIDEO):
                    self._wait_for_runtime(task, "video", "等待本地视频生成运行时恢复")
                    return
                if not self.media_provider.available(AssetKind.IMAGE):
                    self._wait_for_runtime(task, "image", "等待本地图像生成运行时恢复")
                    return
                if not task.audio_path:
                    if not self.tts.available():
                        self._wait_for_runtime(task, "speech", "等待本地配音运行时恢复")
                        return
                    task.audio_path = str(self.tts.synthesize(task.id, task.script))
                    self._record_event(task, "配音", task.status, "本地配音已生成")
                    self.store.save(task)
                for shot in task.content_plan.shots:
                    if shot.status != AssetStatus.PENDING:
                        continue
                    shot.error = None
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
                retrying_assets = (
                    task.metadata.get("automation_retry", {}).get("stage")
                    == TaskStatus.ASSETS_GENERATING.value
                )
                task.status = TaskStatus.ASSETS_GENERATING
                if not retrying_assets:
                    self._complete_stage(task)
                else:
                    task.metadata.pop("waiting_for_runtime", None)
                self._record_event(
                    task,
                    "素材",
                    task.status,
                    f"已提交 {len(task.content_plan.shots)} 个视频分镜和 1 张封面",
                )
                self.store.save(task)
                self._set_run_status(task, "running", "本地媒体素材生成中")

            if task.status == TaskStatus.ASSETS_GENERATING:
                if task.content_plan is None:
                    raise ValueError("content plan is missing")
                if not self.media_provider.available(AssetKind.VIDEO):
                    self._wait_for_runtime(
                        task, "video", "视频生成运行时忙碌或暂不可达，任务将自动续跑"
                    )
                    return
                if not self.media_provider.available(AssetKind.IMAGE):
                    self._wait_for_runtime(
                        task, "image", "图像生成运行时忙碌或暂不可达，任务将自动续跑"
                    )
                    return
                self._mark_runtime_recovered(task)
                self._set_run_status(task, "running", "本地媒体素材生成中")
                for shot in task.content_plan.shots:
                    if shot.status != AssetStatus.QUEUED or not shot.generation_job_id:
                        continue
                    result = self.media_provider.poll(shot.generation_job_id, AssetKind.VIDEO)
                    if result.error or result.state == "failed":
                        shot.status = AssetStatus.FAILED
                        shot.error = result.error or "video generation failed"
                        raise ValueError(f"shot {shot.order} failed: {shot.error}")
                    if result.media_path is not None:
                        shot.status = AssetStatus.COMPLETE
                        shot.media_path = str(result.media_path)
                        self._record_event(
                            task,
                            "素材",
                            task.status,
                            f"分镜 {shot.order} 已生成",
                        )
                cover_job_id = str(task.metadata.get("cover_generation_job_id") or "")
                if cover_job_id and task.metadata.get("cover_status") != "complete":
                    cover = self.media_provider.poll(cover_job_id, AssetKind.IMAGE)
                    if cover.error or cover.state == "failed":
                        task.metadata["cover_status"] = AssetStatus.FAILED.value
                        raise ValueError(cover.error or "cover generation failed")
                    if cover.media_path is not None:
                        task.cover_path = str(cover.media_path)
                        task.metadata["cover_status"] = "complete"
                        self._record_event(task, "素材", task.status, "视频封面已生成")
                shots_complete = all(
                    shot.status == AssetStatus.COMPLETE for shot in task.content_plan.shots
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
                self._complete_stage(task)
                self._record_event(task, "合成", task.status, "全部素材就绪，进入视频合成")
                self.store.save(task)
                self._set_run_status(task, "running", "全部素材已生成，正在合成视频")

            if task.status == TaskStatus.COMPOSING:
                task.generation_job_id = self.mpt.create_video(task)
                task.status = TaskStatus.GENERATED
                self._complete_stage(task)
                self._record_event(
                    task,
                    "合成",
                    task.status,
                    f"视频合成任务已提交：{task.generation_job_id}",
                )
                self.store.save(task)
                self._set_run_status(task, "running", "视频合成任务正在执行")

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
                self._record_event(task, "合成", task.status, "成片已生成，准备自动审核")
                self.store.save(task)

            if task.status == TaskStatus.GENERATED and task.media_path:
                task = self.pipeline.audit(task)
                self._record_event(
                    task,
                    "审核",
                    task.status,
                    f"自动审核完成，得分 {task.audit.score if task.audit else '—'}",
                )
                self.store.save(task)
                if task.status == TaskStatus.REVIEW_REJECTED:
                    self._finish_run(task, "review_rejected", "内容未通过自动审核")
                    return

            if task.status == TaskStatus.APPROVED:
                self._record_event(
                    task, "发布", TaskStatus.PUBLISHING, "审核已通过，进入平台发布门禁"
                )
                task = self.pipeline.publish(task)
                success_count = sum(result.success for result in task.publish_results)
                self._record_event(
                    task,
                    "发布",
                    task.status,
                    f"平台处理完成：{success_count}/{len(task.publish_results)} 成功",
                )
                status = "published" if task.status == TaskStatus.PUBLISHED else "failed"
                self._finish_run(task, status, "自动流程执行完成")
        except MediaRuntimeUnavailableError:
            self._wait_for_runtime(
                task,
                "comfyui",
                "ComfyUI 正在忙碌或暂时无法响应，现有生成作业将自动续跑",
            )
            logger.warning("media runtime temporarily unavailable: task_id=%s", task.id)
        except Exception as exc:
            task.automation_attempts += 1
            task.automation_error = str(exc)[:500]
            will_retry = task.automation_attempts < self.max_attempts
            task.metadata["automation_retry"] = {
                "stage": task.status.value,
                "attempt": task.automation_attempts,
                "max_attempts": self.max_attempts,
                "will_retry": will_retry,
                "next_retry_after_seconds": self.tick_seconds if will_retry else None,
            }
            self._record_event(
                task,
                "异常",
                task.status,
                (
                    f"第 {task.automation_attempts}/{self.max_attempts} 次尝试失败，"
                    f"{'等待自动修复重试' if will_retry else '已达到最大尝试次数'}："
                    f"{task.automation_error}"
                ),
            )
            if not will_retry:
                task.status = TaskStatus.AUTOMATION_FAILED
                self._finish_run(task, "failed", task.automation_error)
            else:
                self._prepare_retry(task)
                self.store.save(task)
            logger.exception("automation task failed: task_id=%s", task.id)

    def cancel_task(self, task_id: str) -> ContentTask:
        task = self.store.get(task_id)
        terminal = {
            TaskStatus.PUBLISHED,
            TaskStatus.REVIEW_REJECTED,
            TaskStatus.PARTIAL_FAILURE,
            TaskStatus.AUTOMATION_FAILED,
            TaskStatus.CANCELLED,
        }
        if task.status in terminal:
            return task
        task.status = TaskStatus.CANCELLED
        task.automation_error = None
        self._record_event(task, "终止", task.status, "用户停止了后续自动编排")
        self._finish_run(task, "cancelled", "用户已停止后续自动编排")
        return task

    def retry_task(self, task_id: str) -> ContentTask:
        task = self.store.get(task_id)
        if task.status != TaskStatus.AUTOMATION_FAILED:
            raise ValueError("only failed automation tasks can be retried")
        failed_stage = str(task.metadata.get("automation_retry", {}).get("stage") or "")
        if failed_stage == TaskStatus.ASSETS_GENERATING.value or (
            task.content_plan and task.audio_path and not task.media_path
        ):
            task.status = TaskStatus.ASSETS_GENERATING
            self._prepare_retry(task, reset_incomplete=True)
        elif task.content_plan is not None:
            task.status = TaskStatus.PLANNED
        else:
            task.status = TaskStatus.DRAFT
        task.automation_attempts = 0
        task.automation_error = None
        task.metadata.pop("automation_retry", None)
        task.metadata.pop("waiting_for_runtime", None)
        self._record_event(
            task, "恢复", task.status, "用户已从失败阶段继续执行，无需重新生成已完成内容"
        )
        self.store.save(task)
        self._set_run_status(task, "queued", "失败任务已恢复，等待本地编排器继续执行")
        return task

    def _prepare_retry(self, task: ContentTask, *, reset_incomplete: bool = False) -> None:
        if task.status != TaskStatus.ASSETS_GENERATING or task.content_plan is None:
            return
        for shot in task.content_plan.shots:
            should_reset = shot.status == AssetStatus.FAILED or (
                reset_incomplete and shot.status != AssetStatus.COMPLETE
            )
            if not should_reset:
                continue
            shot.status = AssetStatus.PENDING
            shot.generation_job_id = None
            shot.provider = None
        cover_status = task.metadata.get("cover_status")
        if cover_status == AssetStatus.FAILED.value or (
            reset_incomplete and cover_status != AssetStatus.COMPLETE.value
        ):
            task.metadata.pop("cover_generation_job_id", None)
            task.metadata.pop("cover_status", None)
        task.status = TaskStatus.PLANNED

    def _wait_for_runtime(self, task: ContentTask, component: str, detail: str) -> None:
        waiting = task.metadata.get("waiting_for_runtime")
        if not isinstance(waiting, dict) or waiting.get("component") != component:
            self._record_event(task, "等待", task.status, detail)
        task.metadata["waiting_for_runtime"] = {"component": component, "detail": detail}
        task.automation_attempts = 0
        task.automation_error = None
        task.metadata.pop("automation_retry", None)
        self.store.save(task)
        self._set_run_status(task, "waiting_for_media_runtime", detail)

    @staticmethod
    def _mark_runtime_recovered(task: ContentTask) -> None:
        transient_error = task.automation_error in {
            "ComfyUI is unreachable",
            "ComfyUI history endpoint is unreachable",
        }
        if task.metadata.get("waiting_for_runtime") or transient_error:
            task.automation_attempts = 0
            task.automation_error = None
            task.metadata.pop("automation_retry", None)
            task.metadata.pop("waiting_for_runtime", None)

    @staticmethod
    def _complete_stage(task: ContentTask) -> None:
        task.automation_attempts = 0
        task.automation_error = None
        task.metadata.pop("automation_retry", None)
        task.metadata.pop("waiting_for_runtime", None)

    @staticmethod
    def _record_event(
        task: ContentTask,
        stage: str,
        status: TaskStatus,
        detail: str,
    ) -> None:
        task.events.append(TaskEvent(stage=stage, status=status.value, detail=detail))

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
