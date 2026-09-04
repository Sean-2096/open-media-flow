from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from redis import Redis

from .lip_sync import LipSyncRuntimeUnavailableError, LocalLipSyncClient
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
    LipSyncStatus,
    ShotPresentationMode,
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
        lip_sync: LocalLipSyncClient,
        *,
        database_url: str,
        redis_url: str,
        timezone: str,
        tick_seconds: int,
        max_attempts: int,
        media_generation_enabled: bool,
        comic_generation_enabled: bool,
        lip_sync_enabled: bool,
        lip_sync_fallback_to_narration: bool,
        lip_sync_min_score: float,
        lip_sync_min_face_coverage: float,
        frame_interpolation_enabled: bool,
        frame_interpolation_multiplier: int,
    ):
        self.store = store
        self.llm_router = llm_router
        self.mpt = mpt
        self.pipeline = pipeline
        self.media_provider = media_provider
        self.tts = tts
        self.lip_sync = lip_sync
        self.tick_seconds = tick_seconds
        self.max_attempts = max_attempts
        self.media_generation_enabled = media_generation_enabled
        self.comic_generation_enabled = comic_generation_enabled
        self.lip_sync_enabled = lip_sync_enabled
        self.lip_sync_fallback_to_narration = lip_sync_fallback_to_narration
        self.lip_sync_min_score = lip_sync_min_score
        self.lip_sync_min_face_coverage = lip_sync_min_face_coverage
        self.frame_interpolation_enabled = frame_interpolation_enabled
        self.frame_interpolation_multiplier = frame_interpolation_multiplier
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
            content_type=automation.content_type,
            presentation_mode=automation.presentation_mode,
            lip_sync_mode=automation.lip_sync_mode,
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
                is_comic = task.content_type.value == "ai_comic"
                comic_seed = (
                    int(hashlib.sha256(task.id.encode()).hexdigest()[:8], 16)
                    if is_comic
                    else -1
                )
                if is_comic and not self.comic_generation_enabled:
                    self._wait_for_runtime(
                        task,
                        "comic_generation",
                        "AI漫剧剧本与分镜已生成，等待安装并启用动漫关键帧模型",
                    )
                    return
                if not is_comic and not self.media_provider.available(AssetKind.VIDEO):
                    self._wait_for_runtime(
                        task,
                        "video",
                        "生成引擎未运行；执行 ./scripts/omf start 后，任务会自动继续",
                    )
                    return
                if not self.media_provider.available(AssetKind.IMAGE):
                    self._wait_for_runtime(
                        task,
                        "image",
                        "生成引擎未运行；执行 ./scripts/omf start 后，任务会自动继续",
                    )
                    return
                if is_comic and not self.tts.comic_renderer_available():
                    self._wait_for_runtime(
                        task,
                        "comic_renderer",
                        "漫剧分镜已规划，等待本地二维镜头渲染器恢复",
                    )
                    return
                if not task.audio_path:
                    if not self.tts.available():
                        self._wait_for_runtime(task, "speech", "等待本地配音运行时恢复")
                        return
                    task.audio_path = str(self.tts.synthesize(task.id, task.script))
                    self._record_event(task, "配音", task.status, "本地配音已生成")
                    self.store.save(task)
                if (
                    not task.metadata.get("character_reference_path")
                    and not task.metadata.get("character_generation_job_id")
                ):
                    reference_prompt = (
                        task.content_plan.character_reference_prompt
                        or task.content_plan.cover_prompt
                    )
                    has_talking_head = any(
                        shot.presentation_mode == ShotPresentationMode.TALKING_HEAD
                        for shot in task.content_plan.shots
                    )
                    if is_comic:
                        character_identity = ", ".join(
                            f"{character.name}, {character.appearance_prompt}, {character.outfit_prompt}"
                            for character in task.content_plan.characters
                        )
                        reference_prompt = (
                            f"{character_identity}, {reference_prompt}, "
                            "clean Chinese animation character design sheet, "
                            "consistent 2D line art, flat color palette, front and side views, "
                            "expression references, full body and waist-up poses, plain background"
                        )
                    elif has_talking_head:
                        reference_prompt = (
                            f"{reference_prompt}, photorealistic adult human presenter, "
                            "front-facing waist-up portrait composition, upper torso visible, hands "
                            "outside the frame, subject occupies about half of the frame, generous headroom, "
                            "direct eye contact, relaxed neutral expression, lips fully sealed "
                            "and pressed together, absolutely no visible teeth, unobstructed mouth, "
                            "symmetrical soft studio lighting, clean simple background"
                        )
                    character_job = self.media_provider.submit(
                        GenerationRequest(
                            task_id=task.id,
                            shot_id="character-reference",
                            kind=AssetKind.IMAGE,
                            prompt=reference_prompt,
                            negative_prompt=(
                                "photorealistic, 3d render, realistic skin, inconsistent character, "
                                "different outfit, text, logo, watermark, extra limbs, extra fingers, "
                                "deformed face, cropped character"
                                if is_comic
                                else
                                "(octane render, drawing, anime, bad photo, bad photography:1.3), "
                                "(worst quality, low quality, blurry:1.2), bad teeth, deformed teeth, "
                                "deformed lips, bad anatomy, bad proportions, deformed eyes, "
                                "deformed face, bad hands, fused fingers, side profile, looking down, "
                                "open mouth, visible teeth, talking, hand over face, microphone, "
                                "camera equipment, text, logo, watermark"
                                if has_talking_head
                                else ""
                            ),
                            width=576 if is_comic else 768 if has_talking_head else 512,
                            height=1024 if has_talking_head or is_comic else 896,
                            seed=comic_seed,
                            workflow_variant="comic" if is_comic else None,
                        )
                    )
                    task.metadata["character_generation_job_id"] = character_job.id
                    task.metadata["character_status"] = AssetStatus.QUEUED.value
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
                    (
                        "已提交漫剧角色设定集；完成后将生成稳定关键帧与二维运镜"
                        if is_comic
                        else "已提交角色母版；完成后将基于同一角色生成全部视频分镜"
                    ),
                )
                self.store.save(task)
                self._set_run_status(task, "running", "本地媒体素材生成中")

            if task.status == TaskStatus.ASSETS_GENERATING:
                if task.content_plan is None:
                    raise ValueError("content plan is missing")
                is_comic = task.content_type.value == "ai_comic"
                comic_seed = (
                    int(hashlib.sha256(task.id.encode()).hexdigest()[:8], 16)
                    if is_comic
                    else -1
                )
                if not is_comic and not self.media_provider.available(AssetKind.VIDEO):
                    self._wait_for_runtime(
                        task, "video", "视频生成运行时忙碌或暂不可达，任务将自动续跑"
                    )
                    return
                if not self.media_provider.available(AssetKind.IMAGE):
                    self._wait_for_runtime(
                        task, "image", "图像生成运行时忙碌或暂不可达，任务将自动续跑"
                    )
                    return
                if is_comic and not self.tts.comic_renderer_available():
                    self._wait_for_runtime(
                        task,
                        "comic_renderer",
                        "关键帧生成就绪，等待本地二维镜头渲染器恢复",
                    )
                    return
                self._mark_runtime_recovered(task)
                self._set_run_status(task, "running", "本地媒体素材生成中")
                character_job_id = str(
                    task.metadata.get("character_generation_job_id") or ""
                )
                character_reference_path = str(
                    task.metadata.get("character_reference_path") or ""
                )
                if character_job_id and not character_reference_path:
                    character = self.media_provider.poll(
                        character_job_id, AssetKind.IMAGE
                    )
                    if character.error or character.state == "failed":
                        task.metadata["character_status"] = AssetStatus.FAILED.value
                        raise ValueError(
                            character.error or "character reference generation failed"
                        )
                    if character.media_path is None:
                        self.store.save(task)
                        return
                    character_reference_path = str(character.media_path)
                    task.metadata["character_reference_path"] = character_reference_path
                    task.metadata["character_status"] = AssetStatus.COMPLETE.value
                    task.cover_path = character_reference_path
                    task.metadata["cover_status"] = AssetStatus.COMPLETE.value
                    for shot in task.content_plan.shots:
                        if shot.presentation_mode == ShotPresentationMode.TALKING_HEAD:
                            shot.lip_sync_source_path = character_reference_path
                    self._record_event(
                        task,
                        "角色一致性",
                        task.status,
                        (
                            "漫剧角色设定集已生成；后续分镜将复用固定外观和画风描述"
                            if is_comic
                            else "角色母版已生成；讲话分镜将使用稳定闭口正脸作为口型底片"
                        ),
                    )
                    self.store.save(task)

                if character_reference_path:
                    for shot in task.content_plan.shots:
                        if shot.presentation_mode == ShotPresentationMode.TALKING_HEAD:
                            shot.lip_sync_source_path = character_reference_path

                submitted_shots = 0
                for shot in task.content_plan.shots:
                    if shot.status != AssetStatus.PENDING:
                        continue
                    shot.error = None
                    shot_prompt = shot.visual_prompt
                    if is_comic:
                        cast = [
                            character
                            for character in task.content_plan.characters
                            if character.id in shot.characters
                            or character.name in shot.characters
                        ]
                        identity_tags = ", ".join(
                            f"{character.name}, {character.appearance_prompt}, {character.outfit_prompt}"
                            for character in cast
                        )
                        if identity_tags:
                            shot_prompt = f"{identity_tags}, {shot_prompt}"
                    job = self.media_provider.submit(
                        GenerationRequest(
                            task_id=task.id,
                            shot_id=shot.id,
                            kind=shot.kind,
                            prompt=shot_prompt,
                            negative_prompt=shot.negative_prompt,
                            duration_seconds=shot.duration_seconds,
                            width=576 if is_comic else 512,
                            height=1024 if is_comic else 896,
                            seed=comic_seed,
                            reference_image_path=character_reference_path or None,
                            workflow_variant="comic" if is_comic else None,
                        )
                    )
                    shot.provider = job.provider
                    shot.generation_job_id = job.id
                    shot.status = AssetStatus.QUEUED
                    submitted_shots += 1
                    self.store.save(task)
                if submitted_shots:
                    mode = (
                        "漫剧关键帧"
                        if is_comic
                        else "角色母版图生视频" if character_reference_path else "文生视频"
                    )
                    self._record_event(
                        task,
                        "角色一致性",
                        task.status,
                        f"已按{mode}提交 {submitted_shots} 个分镜",
                    )
                    self.store.save(task)
                for shot in task.content_plan.shots:
                    if shot.status != AssetStatus.QUEUED or not shot.generation_job_id:
                        continue
                    result = self.media_provider.poll(shot.generation_job_id, shot.kind)
                    if result.error or result.state == "failed":
                        shot.status = AssetStatus.FAILED
                        shot.error = result.error or "video generation failed"
                        raise ValueError(f"shot {shot.order} failed: {shot.error}")
                    if result.media_path is not None:
                        shot.status = AssetStatus.COMPLETE
                        if is_comic:
                            shot.keyframe_path = str(result.media_path)
                            rendered, renderer = self.tts.render_comic_shot(
                                task.id,
                                shot.id,
                                shot.keyframe_path,
                                duration_seconds=shot.duration_seconds,
                                motion=shot.camera_motion.value,
                            )
                            shot.media_path = str(rendered)
                            shot.provider = f"{shot.provider}+{renderer}"
                        else:
                            shot.media_path = str(result.media_path)
                        self._record_event(
                            task,
                            "素材",
                            task.status,
                            (
                                f"漫剧分镜 {shot.order} 关键帧与{shot.camera_motion.value}运镜已生成"
                                if is_comic
                                else f"分镜 {shot.order} 已生成"
                            ),
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
                self._start_lip_sync_or_finish(task)

            if task.status == TaskStatus.LIP_SYNCING:
                if task.content_plan is None:
                    raise ValueError("content plan is missing")
                if not self.lip_sync.available(task.lip_sync_mode.value):
                    self._wait_for_runtime(
                        task,
                        "lip_sync",
                        (
                            "正面讲话分镜已就绪，等待 LatentSync 高质量运行时恢复"
                            if task.lip_sync_mode.value == "quality"
                            else "正面讲话分镜已就绪，等待可用的唇形同步运行时恢复"
                        ),
                    )
                    return
                self._mark_runtime_recovered(task)
                for shot in task.content_plan.shots:
                    if shot.lip_sync_status != LipSyncStatus.PENDING:
                        continue
                    if not shot.media_path or not shot.audio_path:
                        raise ValueError(
                            f"shot {shot.order} lip-sync inputs are incomplete"
                        )
                    job = self.lip_sync.submit(
                        task.id,
                        shot.id,
                        shot.lip_sync_source_path or shot.media_path,
                        shot.audio_path,
                        task.lip_sync_mode.value,
                    )
                    shot.lip_sync_job_id = job.id
                    shot.lip_sync_provider = job.provider
                    shot.lip_sync_status = LipSyncStatus.QUEUED
                    self.store.save(task)
                progresses: list[int] = []
                lip_sync_stages: list[str] = []
                lip_sync_elapsed: list[int] = []
                for shot in task.content_plan.shots:
                    if shot.lip_sync_status != LipSyncStatus.QUEUED:
                        continue
                    if not shot.lip_sync_job_id:
                        raise ValueError(f"shot {shot.order} lip-sync job id is missing")
                    result = self.lip_sync.poll(shot.lip_sync_job_id)
                    progresses.append(result.progress)
                    if result.stage:
                        lip_sync_stages.append(result.stage)
                    if result.elapsed_seconds is not None:
                        lip_sync_elapsed.append(result.elapsed_seconds)
                    if result.error or result.state == "failed":
                        self._handle_lip_sync_failure(
                            task,
                            shot,
                            result.error or "lip-sync generation failed",
                        )
                        continue
                    if result.media_path is None:
                        continue
                    quality_error = self._lip_sync_quality_error(
                        result.sync_score,
                        result.face_coverage,
                    )
                    if quality_error:
                        self._handle_lip_sync_failure(task, shot, quality_error)
                        continue
                    shot.media_path = str(result.media_path)
                    shot.lip_sync_status = LipSyncStatus.COMPLETE
                    shot.effective_presentation_mode = ShotPresentationMode.TALKING_HEAD
                    shot.lip_sync_score = result.sync_score
                    shot.face_coverage = result.face_coverage
                    shot.lip_sync_error = None
                    self._record_event(
                        task,
                        "口型",
                        task.status,
                        f"分镜 {shot.order} 已通过口型质量门禁",
                    )
                if progresses:
                    task.metadata["lip_sync_progress"] = round(
                        sum(progresses) / len(progresses)
                    )
                if lip_sync_stages:
                    task.metadata["lip_sync_stage"] = lip_sync_stages[0]
                if lip_sync_elapsed:
                    task.metadata["lip_sync_elapsed_seconds"] = max(lip_sync_elapsed)
                pending_lip_sync = any(
                    shot.lip_sync_status == LipSyncStatus.QUEUED
                    for shot in task.content_plan.shots
                )
                if pending_lip_sync:
                    self.store.save(task)
                    return
                talking_shots = [
                    shot
                    for shot in task.content_plan.shots
                    if shot.presentation_mode == ShotPresentationMode.TALKING_HEAD
                ]
                task.metadata["lip_sync_summary"] = {
                    "requested": len(talking_shots),
                    "completed": sum(
                        shot.lip_sync_status == LipSyncStatus.COMPLETE
                        for shot in talking_shots
                    ),
                    "fallback": sum(
                        shot.lip_sync_status == LipSyncStatus.SKIPPED
                        for shot in talking_shots
                    ),
                    "runtime": "configured",
                }
                self._finish_assets(task)

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
        except (MediaRuntimeUnavailableError, LipSyncRuntimeUnavailableError):
            is_lip_sync = task.status == TaskStatus.LIP_SYNCING
            self._wait_for_runtime(
                task,
                "lip_sync" if is_lip_sync else "generation_engine",
                (
                    "唇形同步运行时暂时无法响应，现有作业会保留并自动续跑"
                    if is_lip_sync
                    else "生成引擎暂时无法响应，现有作业会保留并自动续跑"
                ),
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

    def _start_lip_sync_or_finish(self, task: ContentTask) -> None:
        if task.content_plan is None:
            raise ValueError("content plan is missing")
        talking_shots = [
            shot
            for shot in task.content_plan.shots
            if shot.presentation_mode == ShotPresentationMode.TALKING_HEAD
        ]
        for shot in task.content_plan.shots:
            if shot.presentation_mode == ShotPresentationMode.NARRATION:
                shot.effective_presentation_mode = ShotPresentationMode.NARRATION
                shot.lip_sync_status = LipSyncStatus.SKIPPED
        if not talking_shots:
            self._finish_assets(task)
            return

        for shot in talking_shots:
            if not shot.audio_path:
                shot.audio_path = str(
                    self.tts.synthesize(
                        task.id,
                        shot.narration,
                        clip_id=f"shot-{shot.id}",
                    )
                )
                self._record_event(
                    task,
                    "配音",
                    task.status,
                    f"分镜 {shot.order} 独立口型驱动音频已生成",
                )
                self.store.save(task)

        if not getattr(self, "lip_sync_enabled", False):
            for shot in talking_shots:
                self._fallback_lip_sync(
                    shot,
                    "本地唇形模型尚未安装，已安全降级为旁白镜头",
                )
            task.metadata["lip_sync_summary"] = {
                "requested": len(talking_shots),
                "completed": 0,
                "fallback": len(talking_shots),
                "runtime": "not_installed",
            }
            self._record_event(
                task,
                "口型",
                task.status,
                f"{len(talking_shots)} 个讲话分镜已降级为旁白；安装唇形模型后将自动启用",
            )
            self._finish_assets(task)
            return

        task.status = TaskStatus.LIP_SYNCING
        task.metadata["lip_sync_progress"] = 0
        self._complete_stage(task)
        self._record_event(
            task,
            "口型",
            task.status,
            f"准备为 {len(talking_shots)} 个正面讲话分镜执行音频驱动口型同步",
        )
        self.store.save(task)
        self._set_run_status(task, "running", "正在执行分镜级唇形同步与质量门禁")

    def _handle_lip_sync_failure(self, task: ContentTask, shot, reason: str) -> None:
        shot.lip_sync_status = LipSyncStatus.FAILED
        shot.lip_sync_error = reason
        if not getattr(self, "lip_sync_fallback_to_narration", True):
            raise ValueError(f"shot {shot.order} lip-sync failed: {reason}")
        self._fallback_lip_sync(shot, reason)
        self._record_event(
            task,
            "口型",
            task.status,
            f"分镜 {shot.order} 未通过口型门禁，已自动降级为旁白镜头：{reason}",
        )

    @staticmethod
    def _fallback_lip_sync(shot, reason: str) -> None:
        shot.lip_sync_status = LipSyncStatus.SKIPPED
        shot.effective_presentation_mode = ShotPresentationMode.NARRATION
        shot.lip_sync_fallback_reason = reason[:500]

    def _lip_sync_quality_error(
        self,
        sync_score: float | None,
        face_coverage: float | None,
    ) -> str | None:
        if sync_score is None or face_coverage is None:
            return "唇形运行时未返回同步分数或正脸覆盖率"
        min_sync = getattr(self, "lip_sync_min_score", 0.65)
        min_face = getattr(self, "lip_sync_min_face_coverage", 0.80)
        if sync_score < min_sync:
            return f"口型同步分数 {sync_score:.2f} 低于门槛 {min_sync:.2f}"
        if face_coverage < min_face:
            return f"正脸有效覆盖率 {face_coverage:.2f} 低于门槛 {min_face:.2f}"
        return None

    def _finish_assets(self, task: ContentTask) -> None:
        if task.content_plan is None:
            raise ValueError("content plan is missing")
        interpolate_shots = (
            self.frame_interpolation_enabled and task.content_type.value != "ai_comic"
        )
        if interpolate_shots:
            if not self.tts.interpolation_available():
                self._wait_for_runtime(
                    task,
                    "frame_interpolation",
                    "分镜已生成，等待本地 RIFE 插帧运行时恢复",
                )
                return
            interpolation = task.metadata.setdefault("frame_interpolation", {})
            for shot in task.content_plan.shots:
                if shot.id in interpolation:
                    shot.media_path = interpolation[shot.id]["output"]
                    continue
                if not shot.media_path:
                    raise ValueError(f"shot {shot.order} media path is missing")
                source = shot.media_path
                output, provider = self.tts.interpolate(
                    task.id,
                    shot.id,
                    source,
                    multiplier=self.frame_interpolation_multiplier,
                )
                shot.media_path = str(output)
                interpolation[shot.id] = {
                    "source": source,
                    "output": str(output),
                    "provider": provider,
                    "multiplier": self.frame_interpolation_multiplier,
                }
                self._record_event(
                    task,
                    "流畅度",
                    task.status,
                    f"分镜 {shot.order} 已完成 {self.frame_interpolation_multiplier} 倍 AI 插帧",
                )
                self.store.save(task)
        task.video_materials = [
            shot.media_path
            for shot in task.content_plan.shots
            if shot.media_path is not None
        ]
        task.status = TaskStatus.COMPOSING
        self._complete_stage(task)
        detail = "全部素材就绪，进入视频合成"
        if interpolate_shots:
            detail = "全部素材与 AI 插帧已就绪，进入 48 FPS 视频合成"
        self._record_event(task, "合成", task.status, detail)
        self.store.save(task)
        self._set_run_status(task, "running", "全部素材已生成，正在合成视频")

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
        if failed_stage == TaskStatus.LIP_SYNCING.value:
            task.status = TaskStatus.LIP_SYNCING
            self._prepare_retry(task, reset_incomplete=True)
        elif failed_stage == TaskStatus.ASSETS_GENERATING.value or (
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
        if task.status == TaskStatus.LIP_SYNCING and task.content_plan is not None:
            for shot in task.content_plan.shots:
                should_reset = shot.lip_sync_status == LipSyncStatus.FAILED or (
                    reset_incomplete
                    and shot.presentation_mode == ShotPresentationMode.TALKING_HEAD
                    and shot.lip_sync_status != LipSyncStatus.COMPLETE
                )
                if not should_reset:
                    continue
                shot.lip_sync_status = LipSyncStatus.PENDING
                shot.lip_sync_job_id = None
                shot.lip_sync_provider = None
                shot.lip_sync_error = None
            return
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
        character_status = task.metadata.get("character_status")
        if character_status == AssetStatus.FAILED.value:
            task.metadata.pop("character_generation_job_id", None)
            task.metadata.pop("character_status", None)
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
