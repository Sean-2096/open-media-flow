from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class Platform(StrEnum):
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"
    BILIBILI = "bilibili"
    YOUTUBE = "youtube"


class PresentationMode(StrEnum):
    NARRATION = "narration"
    MIXED = "mixed"
    TALKING_HEAD = "talking_head"


class LipSyncMode(StrEnum):
    AUTO = "auto"
    FAST = "fast"
    QUALITY = "quality"


class ShotPresentationMode(StrEnum):
    NARRATION = "narration"
    TALKING_HEAD = "talking_head"


class LipSyncStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskStatus(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    ASSETS_GENERATING = "assets_generating"
    LIP_SYNCING = "lip_syncing"
    COMPOSING = "composing"
    GENERATED = "generated"
    REVIEW_REJECTED = "review_rejected"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PARTIAL_FAILURE = "partial_failure"
    AUTOMATION_FAILED = "automation_failed"
    CANCELLED = "cancelled"


class TaskCreate(BaseModel):
    topic: str = Field(min_length=2, max_length=500)
    platforms: list[Platform] = Field(min_length=1)
    title: str = Field(default="", max_length=200)
    script: str = Field(default="", max_length=10_000)
    description: str = Field(default="", max_length=5_000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    video_materials: list[str] = Field(default_factory=list, max_length=100)
    presentation_mode: PresentationMode = PresentationMode.NARRATION
    lip_sync_mode: LipSyncMode = LipSyncMode.AUTO
    contains_synthetic_media: bool = True


class AssetStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    COMPLETE = "complete"
    FAILED = "failed"


class AssetKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class ShotSpec(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    order: int = Field(ge=1, le=30)
    narration: str = Field(min_length=5, max_length=500)
    visual_prompt: str = Field(min_length=10, max_length=2_000)
    negative_prompt: str = Field(default="", max_length=1_000)
    duration_seconds: int = Field(default=5, ge=2, le=15)
    kind: AssetKind = AssetKind.VIDEO
    presentation_mode: ShotPresentationMode = ShotPresentationMode.NARRATION
    effective_presentation_mode: ShotPresentationMode | None = None
    provider: str | None = None
    status: AssetStatus = AssetStatus.PENDING
    generation_job_id: str | None = None
    media_path: str | None = None
    audio_path: str | None = None
    lip_sync_source_path: str | None = None
    lip_sync_status: LipSyncStatus = LipSyncStatus.PENDING
    lip_sync_job_id: str | None = None
    lip_sync_provider: str | None = None
    lip_sync_score: float | None = Field(default=None, ge=0, le=1)
    face_coverage: float | None = Field(default=None, ge=0, le=1)
    lip_sync_error: str | None = None
    lip_sync_fallback_reason: str | None = None
    error: str | None = None


class ContentPlan(BaseModel):
    audience: str = Field(min_length=2, max_length=300)
    hook: str = Field(min_length=5, max_length=300)
    creative_direction: str = Field(min_length=5, max_length=1_000)
    cover_prompt: str = Field(min_length=10, max_length=2_000)
    character_reference_prompt: str = Field(default="", max_length=2_000)
    shots: list[ShotSpec] = Field(min_length=3, max_length=12)


class MediaAttach(BaseModel):
    media_path: str


class AuditCheck(BaseModel):
    name: str
    passed: bool
    score: int = Field(ge=0, le=100)
    detail: str


class AuditReport(BaseModel):
    approved: bool
    score: int = Field(ge=0, le=100)
    checks: list[AuditCheck]
    reviewed_at: datetime = Field(default_factory=utc_now)


class PublishResult(BaseModel):
    platform: Platform
    success: bool
    remote_id: str | None = None
    detail: str
    published_at: datetime = Field(default_factory=utc_now)


class TaskEvent(BaseModel):
    stage: str
    status: str
    detail: str
    created_at: datetime = Field(default_factory=utc_now)


class ContentTask(TaskCreate):
    id: str = Field(default_factory=lambda: uuid4().hex)
    status: TaskStatus = TaskStatus.DRAFT
    media_path: str | None = None
    generation_job_id: str | None = None
    automation_id: str | None = None
    automation_run_id: str | None = None
    automation_attempts: int = 0
    automation_error: str | None = None
    content_plan: ContentPlan | None = None
    cover_path: str | None = None
    audio_path: str | None = None
    audit: AuditReport | None = None
    publish_results: list[PublishResult] = Field(default_factory=list)
    events: list[TaskEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AutomationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    topic: str = Field(min_length=2, max_length=500)
    platforms: list[Platform] = Field(min_length=1)
    video_materials: list[str] = Field(default_factory=list, max_length=100)
    presentation_mode: PresentationMode = PresentationMode.NARRATION
    lip_sync_mode: LipSyncMode = LipSyncMode.AUTO
    interval_minutes: int = Field(default=1440, ge=1, le=525_600)
    enabled: bool = True


class Automation(AutomationCreate):
    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_run_at: datetime | None = None


class AutomationRun(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    automation_id: str
    task_id: str
    status: str = "queued"
    detail: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
