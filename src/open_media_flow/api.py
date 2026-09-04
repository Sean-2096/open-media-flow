from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .audit import ContentAuditor
from .llm import FallbackLLMRouter, LLMError, OpenAICompatibleClient
from .lip_sync import LocalLipSyncClient
from .media import UnsafeMediaPathError, resolve_media_path
from .media_providers import ComfyUIProvider
from .models import (
    AssetKind,
    Automation,
    AutomationCreate,
    AutomationRun,
    ContentTask,
    MediaAttach,
    TaskCreate,
    TaskStatus,
)
from .mpt import MoneyPrinterClient
from .orchestrator import AutomationAlreadyRunningError, AutomationEngine
from .pipeline import Pipeline
from .publishers import DryRunPublisher
from .settings import settings
from .store import (
    AutomationNotFoundError,
    JsonTaskStore,
    PostgresStore,
    TaskNotFoundError,
)
from .tts import LocalTTSClient

store = (
    PostgresStore(settings.database_url)
    if settings.store_backend == "postgres"
    else JsonTaskStore(settings.data_dir / "state" / "tasks.json")
)
if isinstance(store, PostgresStore):
    legacy_store = JsonTaskStore(settings.data_dir / "state" / "tasks.json")
    for legacy_task in legacy_store.list():
        try:
            store.get(legacy_task.id)
        except TaskNotFoundError:
            store.create(legacy_task)
policy_path = Path(__file__).resolve().parents[2] / "config" / "policy.json"
if not policy_path.exists():
    policy_path = Path("/app/config/policy.json")
primary_llm = OpenAICompatibleClient(
    settings.llm_primary,
    timeout_seconds=settings.llm_request_timeout_seconds,
    openrouter_zdr=settings.openrouter_zdr,
    openrouter_data_collection=settings.openrouter_data_collection,
)
fallback_llm = None
if settings.llm_fallback_enabled:
    fallback_llm = OpenAICompatibleClient(
        settings.llm_fallback,
        timeout_seconds=settings.llm_request_timeout_seconds,
        openrouter_zdr=settings.openrouter_zdr,
        openrouter_data_collection=settings.openrouter_data_collection,
    )
llm_router = FallbackLLMRouter(
    primary_llm,
    fallback_llm,
    primary_attempts=settings.llm_primary_attempts,
    fallback_review_min_score=settings.llm_fallback_review_min_score,
    fallback_review_max_score=settings.llm_fallback_review_max_score,
)
auditor = ContentAuditor(policy_path, llm_reviewer=llm_router)
pipeline = Pipeline(store, auditor, DryRunPublisher())
mpt = MoneyPrinterClient(
    settings.mpt_base_url,
    settings.data_dir / "inbox",
    settings.mpt_api_key,
    settings.mpt_output_dir,
)
media_provider = ComfyUIProvider(
    settings.comfyui_base_url,
    settings.comfyui_output_dir,
    settings.comfyui_image_workflow,
    settings.comfyui_video_workflow,
    video_i2v_workflow=settings.comfyui_video_i2v_workflow,
)
tts = LocalTTSClient(
    settings.local_media_runtime_base_url,
    settings.local_media_runtime_api_key,
    settings.data_dir / "inbox",
)
lip_sync = LocalLipSyncClient(
    settings.lip_sync_base_url,
    settings.lip_sync_api_key,
    settings.data_dir / "inbox",
)
automation_engine = None
if settings.scheduler_enabled:
    if not isinstance(store, PostgresStore):
        raise RuntimeError("the built-in scheduler requires OMF_STORE_BACKEND=postgres")
    automation_engine = AutomationEngine(
        store,
        llm_router,
        mpt,
        pipeline,
        media_provider,
        tts,
        lip_sync,
        database_url=settings.database_url,
        redis_url=settings.redis_url,
        timezone=settings.scheduler_timezone,
        tick_seconds=settings.scheduler_tick_seconds,
        max_attempts=settings.automation_max_attempts,
        media_generation_enabled=settings.media_generation_enabled,
        lip_sync_enabled=settings.lip_sync_enabled,
        lip_sync_fallback_to_narration=settings.lip_sync_fallback_to_narration,
        lip_sync_min_score=settings.lip_sync_min_score,
        lip_sync_min_face_coverage=settings.lip_sync_min_face_coverage,
        frame_interpolation_enabled=settings.frame_interpolation_enabled,
        frame_interpolation_multiplier=settings.frame_interpolation_multiplier,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if automation_engine is not None:
        automation_engine.start()
    try:
        yield
    finally:
        if automation_engine is not None:
            automation_engine.stop()


app = FastAPI(title="OpenMediaFlow", version="0.7.0", lifespan=lifespan)
web_root = Path(__file__).resolve().parent / "web"
app.mount("/assets", StaticFiles(directory=web_root), name="dashboard-assets")
dashboard_cookie_name = "omf_dashboard_session"
_runtime_health_cache: tuple[float, dict[str, bool]] | None = None
_runtime_last_success: dict[str, float] = {}


def _probe_json(url: str, *, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            if response.status != 200:
                return {}
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return {}


def _probe_http(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def runtime_health() -> dict[str, bool]:
    """Return real runtime readiness without making every UI poll fan out."""
    global _runtime_health_cache
    now = time.monotonic()
    if _runtime_health_cache and now - _runtime_health_cache[0] < 5:
        return _runtime_health_cache[1]

    model_online = bool(_probe_json(f"{settings.llm_primary.base_url}/models"))
    generation_online = bool(
        _probe_json(f"{settings.comfyui_base_url}/system_stats")
    )
    native_health = _probe_json(
        f"{settings.local_media_runtime_base_url}/health",
        headers={"X-API-Key": settings.local_media_runtime_api_key},
    )
    compositor_online = _probe_http(f"{settings.mpt_base_url}/ping")
    observed = {
        "content_model_ready": model_online,
        "generation_engine_ready": generation_online
        and settings.comfyui_image_workflow.is_file()
        and settings.comfyui_video_workflow.is_file()
        and settings.comfyui_video_i2v_workflow.is_file(),
        "voice_engine_ready": native_health.get("tts_ready", True) is not False
        and bool(native_health),
        "motion_engine_ready": native_health.get(
            "frame_interpolation_ready", False
        )
        is True,
        "lip_sync_engine_ready": settings.lip_sync_enabled and lip_sync.available(),
        "video_compositor_ready": compositor_online,
    }
    result = {}
    for component, ready in observed.items():
        if ready:
            _runtime_last_success[component] = now
        if component == "generation_engine_ready":
            result[component] = ready
        else:
            result[component] = (
                ready or now - _runtime_last_success.get(component, 0) < 180
            )
    _runtime_health_cache = (now, result)
    return result


def dashboard_session_token() -> str:
    return hmac.new(
        settings.api_key.encode(),
        b"open-media-flow-dashboard",
        hashlib.sha256,
    ).hexdigest()


def is_same_origin_dashboard_request(request: Request) -> bool:
    if request.headers.get("sec-fetch-site") == "same-origin":
        return True
    expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    return origin == expected_origin or referer.startswith(f"{expected_origin}/")


def require_api_key(
    request: Request,
    x_api_key: str = Header(default=""),
    dashboard_session: str = Cookie(default="", alias=dashboard_cookie_name),
) -> None:
    if not settings.api_key:
        return
    header_is_valid = hmac.compare_digest(x_api_key, settings.api_key)
    session_is_valid = is_same_origin_dashboard_request(request) and hmac.compare_digest(
        dashboard_session, dashboard_session_token()
    )
    if not header_is_valid and not session_is_valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


@app.get("/", include_in_schema=False, response_class=FileResponse)
def dashboard() -> FileResponse:
    response = FileResponse(web_root / "index.html")
    if settings.api_key:
        response.set_cookie(
            key=dashboard_cookie_name,
            value=dashboard_session_token(),
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="strict",
        )
    return response


def get_task(task_id: str) -> ContentTask:
    try:
        return store.get(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


def invalidate_review(task: ContentTask) -> None:
    task.audit = None
    task.publish_results = []


def refresh_manual_video_task(task: ContentTask) -> ContentTask:
    if task.automation_id or not task.generation_job_id or task.media_path:
        return task
    try:
        video = mpt.get_video_status(task.generation_job_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=502, detail=f"MoneyPrinterTurbo status request failed: {exc}"
        ) from exc

    previous_progress = task.metadata.get("video_progress")
    task.metadata["video_progress"] = video.progress
    changed = previous_progress != video.progress
    if video.error and task.automation_error != video.error:
        task.automation_error = video.error
        changed = True
    elif video.media_path is not None:
        task.media_path = str(video.media_path)
        task.automation_error = None
        changed = True
    return store.save(task) if changed else task


def media_file_response(value: str | None, label: str) -> FileResponse:
    if not value:
        raise HTTPException(status_code=404, detail=f"{label} has not been generated")
    try:
        path = resolve_media_path(value, settings.allowed_media_roots)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"{label} not found") from exc
    except UnsafeMediaPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@app.get("/health")
def health() -> dict:
    components = runtime_health()
    native_health = _probe_json(
        f"{settings.local_media_runtime_base_url}/health",
        headers={"X-API-Key": settings.local_media_runtime_api_key},
    )
    generation_ready = (
        settings.media_generation_enabled
        and components["generation_engine_ready"]
        and components["voice_engine_ready"]
        and (
            not settings.frame_interpolation_enabled
            or components["motion_engine_ready"]
        )
    )
    control_ready = bool(automation_engine and automation_engine.running)
    return {
        "status": "ok" if control_ready and generation_ready else "degraded",
        "publish_mode": settings.publish_mode,
        "llm_primary_model": settings.llm_primary.model,
        "llm_fallback_enabled": settings.llm_fallback_enabled,
        "llm_primary_attempts": settings.llm_primary_attempts,
        "automation_max_attempts": settings.automation_max_attempts,
        "scheduler_enabled": settings.scheduler_enabled,
        "scheduler_running": bool(automation_engine and automation_engine.running),
        "store_backend": settings.store_backend,
        "media_generation_enabled": settings.media_generation_enabled,
        "media_video_provider": settings.media_video_provider,
        "lip_sync_enabled": settings.lip_sync_enabled,
        "lip_sync_fallback_to_narration": settings.lip_sync_fallback_to_narration,
        "lip_sync_engines": native_health.get("lip_sync_engines", []),
        "lip_sync_modes": native_health.get("lip_sync_modes", []),
        "generation_ready": generation_ready,
        "components": components,
    }


@app.get("/media-runtime", dependencies=[Depends(require_api_key)])
def media_runtime() -> dict[str, object]:
    return {
        "provider": media_provider.name,
        "base_url": settings.comfyui_base_url,
        "image_workflow_configured": settings.comfyui_image_workflow.is_file(),
        "video_workflow_configured": settings.comfyui_video_workflow.is_file(),
        "video_i2v_workflow_configured": settings.comfyui_video_i2v_workflow.is_file(),
        "image_available": media_provider.available(AssetKind.IMAGE),
        "video_available": media_provider.available(AssetKind.VIDEO),
        "speech_runtime": settings.local_media_runtime_base_url,
        "speech_available": tts.available(),
        "lip_sync_enabled": settings.lip_sync_enabled,
        "lip_sync_runtime": settings.lip_sync_base_url,
        "lip_sync_available": lip_sync.available(),
        "lip_sync_engines": _probe_json(
            f"{settings.local_media_runtime_base_url}/health",
            headers={"X-API-Key": settings.local_media_runtime_api_key},
        ).get("lip_sync_engines", []),
    }


@app.post("/tasks", response_model=ContentTask, dependencies=[Depends(require_api_key)])
def create_task(body: TaskCreate) -> ContentTask:
    return store.create(ContentTask(**body.model_dump()))


@app.get("/tasks", response_model=list[ContentTask], dependencies=[Depends(require_api_key)])
def list_tasks() -> list[ContentTask]:
    return store.list()


def require_automation_engine() -> AutomationEngine:
    if automation_engine is None:
        raise HTTPException(status_code=503, detail="built-in scheduler is disabled")
    return automation_engine


@app.post(
    "/automations",
    response_model=Automation,
    dependencies=[Depends(require_api_key)],
)
def create_automation(body: AutomationCreate) -> Automation:
    return require_automation_engine().create_automation(body)


@app.get(
    "/automations",
    response_model=list[Automation],
    dependencies=[Depends(require_api_key)],
)
def list_automations() -> list[Automation]:
    engine = require_automation_engine()
    return engine.store.list_automations()


@app.put(
    "/automations/{automation_id}",
    response_model=Automation,
    dependencies=[Depends(require_api_key)],
)
def update_automation(automation_id: str, body: AutomationCreate) -> Automation:
    try:
        return require_automation_engine().update_automation(automation_id, body)
    except AutomationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="automation not found") from exc


@app.get(
    "/automation-runs",
    response_model=list[AutomationRun],
    dependencies=[Depends(require_api_key)],
)
def list_automation_runs(automation_id: str | None = None) -> list[AutomationRun]:
    engine = require_automation_engine()
    return engine.store.list_runs(automation_id)


@app.post(
    "/automations/{automation_id}/run",
    response_model=AutomationRun,
    dependencies=[Depends(require_api_key)],
)
def run_automation(automation_id: str) -> AutomationRun:
    try:
        return require_automation_engine().create_task_from_automation(automation_id)
    except AutomationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="automation not found") from exc
    except AutomationAlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail="automation already has an active run") from exc


@app.post(
    "/automations/{automation_id}/enable",
    response_model=Automation,
    dependencies=[Depends(require_api_key)],
)
def enable_automation(automation_id: str) -> Automation:
    try:
        return require_automation_engine().set_enabled(automation_id, True)
    except AutomationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="automation not found") from exc


@app.post(
    "/automations/{automation_id}/disable",
    response_model=Automation,
    dependencies=[Depends(require_api_key)],
)
def disable_automation(automation_id: str) -> Automation:
    try:
        return require_automation_engine().set_enabled(automation_id, False)
    except AutomationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="automation not found") from exc


@app.delete(
    "/automations/{automation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_api_key)],
)
def delete_automation(automation_id: str) -> None:
    try:
        require_automation_engine().delete_automation(automation_id)
    except AutomationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="automation not found") from exc


@app.get("/tasks/{task_id}", response_model=ContentTask, dependencies=[Depends(require_api_key)])
def read_task(task_id: str) -> ContentTask:
    return refresh_manual_video_task(get_task(task_id))


@app.post(
    "/tasks/{task_id}/cancel",
    response_model=ContentTask,
    dependencies=[Depends(require_api_key)],
)
def cancel_task(task_id: str) -> ContentTask:
    try:
        return require_automation_engine().cancel_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@app.post(
    "/tasks/{task_id}/retry",
    response_model=ContentTask,
    dependencies=[Depends(require_api_key)],
)
def retry_task(task_id: str) -> ContentTask:
    try:
        return require_automation_engine().retry_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/tasks/{task_id}/generate-metadata",
    response_model=ContentTask,
    dependencies=[Depends(require_api_key)],
)
def generate_metadata(task_id: str) -> ContentTask:
    task = get_task(task_id)
    try:
        generation = llm_router.generate_metadata(task)
        generated = generation.metadata
        task.title = generated.title
        task.script = generated.script
        task.description = generated.description
        task.tags = generated.tags
        task.metadata["llm_generation"] = {
            "endpoint": generation.endpoint,
            "model": generation.model,
        }
        invalidate_review(task)
        task.status = TaskStatus.GENERATED if task.media_path else TaskStatus.DRAFT
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"LLM generation failed: {exc}") from exc
    return store.save(task)


@app.post(
    "/tasks/{task_id}/generate-content-plan",
    response_model=ContentTask,
    dependencies=[Depends(require_api_key)],
)
def generate_content_plan(task_id: str) -> ContentTask:
    task = get_task(task_id)
    try:
        generation = llm_router.generate_content_plan(task)
        generated = generation.metadata
        task.title = generated.title
        task.script = generated.script
        task.description = generated.description
        task.tags = generated.tags
        task.content_plan = generation.plan
        task.status = TaskStatus.PLANNED
        task.metadata["llm_generation"] = {
            "endpoint": generation.endpoint,
            "model": generation.model,
        }
        invalidate_review(task)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"LLM planning failed: {exc}") from exc
    return store.save(task)


@app.post(
    "/tasks/{task_id}/generate-video",
    response_model=ContentTask,
    dependencies=[Depends(require_api_key)],
)
def generate_video(task_id: str) -> ContentTask:
    task = get_task(task_id)
    if not task.script.strip():
        raise HTTPException(status_code=409, detail="generate or provide a script first")
    try:
        task.generation_job_id = mpt.create_video(task)
        task.media_path = None
        task.automation_error = None
        invalidate_review(task)
        task.status = TaskStatus.GENERATED
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=502, detail=f"MoneyPrinterTurbo request failed: {exc}"
        ) from exc
    return store.save(task)


@app.post(
    "/tasks/{task_id}/media",
    response_model=ContentTask,
    dependencies=[Depends(require_api_key)],
)
def attach_media(task_id: str, body: MediaAttach) -> ContentTask:
    task = get_task(task_id)
    try:
        task.media_path = str(resolve_media_path(body.media_path, settings.allowed_media_roots))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"media not found: {exc}") from exc
    except UnsafeMediaPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    invalidate_review(task)
    task.status = TaskStatus.GENERATED
    return store.save(task)


@app.get(
    "/tasks/{task_id}/preview",
    response_class=FileResponse,
    dependencies=[Depends(require_api_key)],
)
def preview_task_video(task_id: str) -> FileResponse:
    task = refresh_manual_video_task(get_task(task_id))
    return media_file_response(task.media_path, "final video")


@app.get(
    "/tasks/{task_id}/cover",
    response_class=FileResponse,
    dependencies=[Depends(require_api_key)],
)
def preview_task_cover(task_id: str) -> FileResponse:
    return media_file_response(get_task(task_id).cover_path, "cover")


@app.get(
    "/tasks/{task_id}/shots/{shot_id}/preview",
    response_class=FileResponse,
    dependencies=[Depends(require_api_key)],
)
def preview_task_shot(task_id: str, shot_id: str) -> FileResponse:
    task = get_task(task_id)
    shots = task.content_plan.shots if task.content_plan else []
    shot = next((item for item in shots if item.id == shot_id), None)
    if shot is None:
        raise HTTPException(status_code=404, detail="shot not found")
    return media_file_response(shot.media_path, "shot media")


@app.post(
    "/tasks/{task_id}/audit",
    response_model=ContentTask,
    dependencies=[Depends(require_api_key)],
)
def audit_task(task_id: str) -> ContentTask:
    return pipeline.audit(refresh_manual_video_task(get_task(task_id)))


@app.post(
    "/tasks/{task_id}/publish",
    response_model=ContentTask,
    dependencies=[Depends(require_api_key)],
)
def publish_task(task_id: str) -> ContentTask:
    if settings.publish_mode != "dry-run":
        raise HTTPException(
            status_code=501,
            detail="real publishers are not configured; keep dry-run until account authorization is added",
        )
    try:
        return pipeline.publish(get_task(task_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
