from __future__ import annotations

import hashlib
import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .audit import ContentAuditor
from .llm import FallbackLLMRouter, LLMError, OpenAICompatibleClient
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
)
tts = LocalTTSClient(
    settings.local_media_runtime_base_url,
    settings.local_media_runtime_api_key,
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
        database_url=settings.database_url,
        redis_url=settings.redis_url,
        timezone=settings.scheduler_timezone,
        tick_seconds=settings.scheduler_tick_seconds,
        max_attempts=settings.automation_max_attempts,
        media_generation_enabled=settings.media_generation_enabled,
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


app = FastAPI(title="OpenMediaFlow", version="0.6.0", lifespan=lifespan)
web_root = Path(__file__).resolve().parent / "web"
app.mount("/assets", StaticFiles(directory=web_root), name="dashboard-assets")
dashboard_cookie_name = "omf_dashboard_session"


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
    session_is_valid = (
        is_same_origin_dashboard_request(request)
        and hmac.compare_digest(dashboard_session, dashboard_session_token())
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


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "publish_mode": settings.publish_mode,
        "llm_primary_model": settings.llm_primary.model,
        "llm_fallback_enabled": settings.llm_fallback_enabled,
        "scheduler_enabled": settings.scheduler_enabled,
        "scheduler_running": bool(automation_engine and automation_engine.running),
        "store_backend": settings.store_backend,
        "media_generation_enabled": settings.media_generation_enabled,
        "media_video_provider": settings.media_video_provider,
    }


@app.get("/media-runtime", dependencies=[Depends(require_api_key)])
def media_runtime() -> dict[str, str | bool]:
    return {
        "provider": media_provider.name,
        "base_url": settings.comfyui_base_url,
        "image_workflow_configured": settings.comfyui_image_workflow.is_file(),
        "video_workflow_configured": settings.comfyui_video_workflow.is_file(),
        "image_available": media_provider.available(AssetKind.IMAGE),
        "video_available": media_provider.available(AssetKind.VIDEO),
        "speech_runtime": settings.local_media_runtime_base_url,
        "speech_available": tts.available(),
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
    return get_task(task_id)


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
        task.status = TaskStatus.GENERATED
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"MoneyPrinterTurbo request failed: {exc}") from exc
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
    task.status = TaskStatus.GENERATED
    return store.save(task)


@app.post(
    "/tasks/{task_id}/audit",
    response_model=ContentTask,
    dependencies=[Depends(require_api_key)],
)
def audit_task(task_id: str) -> ContentTask:
    return pipeline.audit(get_task(task_id))


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
