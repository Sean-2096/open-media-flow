from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _split_paths(value: str) -> tuple[Path, ...]:
    return tuple(Path(item).expanduser().resolve() for item in value.split(",") if item.strip())


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_optional_bool(value: str) -> bool | None:
    if not value.strip():
        return None
    return _as_bool(value)


@dataclass(frozen=True)
class LLMEndpointSettings:
    name: str
    base_url: str
    model: str
    api_key: str
    enable_thinking: bool | None = None


@dataclass(frozen=True)
class Settings:
    api_key: str
    data_dir: Path
    allowed_media_roots: tuple[Path, ...]
    publish_mode: str
    llm_primary: LLMEndpointSettings
    llm_primary_attempts: int
    llm_request_timeout_seconds: int
    llm_fallback_enabled: bool
    llm_fallback: LLMEndpointSettings
    llm_fallback_review_min_score: int
    llm_fallback_review_max_score: int
    openrouter_zdr: bool
    openrouter_data_collection: str
    mpt_base_url: str
    mpt_api_key: str
    mpt_output_dir: Path
    store_backend: str
    database_url: str
    redis_url: str
    scheduler_enabled: bool
    scheduler_tick_seconds: int
    automation_max_attempts: int
    scheduler_timezone: str
    media_generation_enabled: bool
    media_video_provider: str
    media_image_provider: str
    comfyui_base_url: str
    comfyui_output_dir: Path
    comfyui_image_workflow: Path
    comfyui_video_workflow: Path
    comfyui_video_i2v_workflow: Path
    local_media_runtime_base_url: str
    local_media_runtime_api_key: str
    lip_sync_enabled: bool
    lip_sync_base_url: str
    lip_sync_api_key: str
    lip_sync_fallback_to_narration: bool
    lip_sync_min_score: float
    lip_sync_min_face_coverage: float
    frame_interpolation_enabled: bool
    frame_interpolation_multiplier: int

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.getenv("OMF_DATA_DIR", "data")).expanduser().resolve()
        roots_value = os.getenv(
            "OMF_ALLOWED_MEDIA_ROOTS",
            f"{data_dir / 'inbox'},{data_dir / 'output'}",
        )
        return cls(
            api_key=os.getenv("OMF_API_KEY", "change-me"),
            data_dir=data_dir,
            allowed_media_roots=_split_paths(roots_value),
            publish_mode=os.getenv("OMF_PUBLISH_MODE", "dry-run"),
            llm_primary=LLMEndpointSettings(
                name="primary",
                base_url=os.getenv(
                    "LLM_PRIMARY_BASE_URL", "http://127.0.0.1:8081/v1"
                ).rstrip("/"),
                model=os.getenv("LLM_PRIMARY_MODEL", "Qwen/Qwen3-14B-GGUF"),
                api_key=os.getenv("LLM_PRIMARY_API_KEY", "local"),
                enable_thinking=_as_optional_bool(
                    os.getenv("LLM_PRIMARY_ENABLE_THINKING", "false")
                ),
            ),
            llm_primary_attempts=max(1, int(os.getenv("LLM_PRIMARY_ATTEMPTS", "2"))),
            llm_request_timeout_seconds=max(
                1, int(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "120"))
            ),
            llm_fallback_enabled=_as_bool(os.getenv("LLM_FALLBACK_ENABLED", "false")),
            llm_fallback=LLMEndpointSettings(
                name="fallback",
                base_url=os.getenv(
                    "LLM_FALLBACK_BASE_URL", "https://openrouter.ai/api/v1"
                ).rstrip("/"),
                model=os.getenv("LLM_FALLBACK_MODEL", ""),
                api_key=os.getenv("LLM_FALLBACK_API_KEY", ""),
                enable_thinking=_as_optional_bool(
                    os.getenv("LLM_FALLBACK_ENABLE_THINKING", "")
                ),
            ),
            llm_fallback_review_min_score=max(
                0, int(os.getenv("LLM_FALLBACK_REVIEW_MIN_SCORE", "70"))
            ),
            llm_fallback_review_max_score=min(
                100, int(os.getenv("LLM_FALLBACK_REVIEW_MAX_SCORE", "84"))
            ),
            openrouter_zdr=_as_bool(os.getenv("OPENROUTER_ZDR", "true")),
            openrouter_data_collection=os.getenv(
                "OPENROUTER_DATA_COLLECTION", "deny"
            ).strip(),
            mpt_base_url=os.getenv("MPT_BASE_URL", "http://video-engine:8080").rstrip("/"),
            mpt_api_key=os.getenv("MPT_API_KEY", ""),
            mpt_output_dir=Path(
                os.getenv("MPT_OUTPUT_DIR", str(data_dir / "output/video-engine"))
            ).expanduser().resolve(),
            store_backend=os.getenv("OMF_STORE_BACKEND", "json").strip().lower(),
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql+psycopg://open_media_flow:local-dev-only@postgres:5432/"
                "open_media_flow",
            ),
            redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
            scheduler_enabled=_as_bool(os.getenv("OMF_SCHEDULER_ENABLED", "false")),
            scheduler_tick_seconds=max(
                2, int(os.getenv("OMF_SCHEDULER_TICK_SECONDS", "15"))
            ),
            automation_max_attempts=max(
                1, int(os.getenv("OMF_AUTOMATION_MAX_ATTEMPTS", "3"))
            ),
            scheduler_timezone=os.getenv("OMF_SCHEDULER_TIMEZONE", "Asia/Shanghai"),
            media_generation_enabled=_as_bool(
                os.getenv("OMF_MEDIA_GENERATION_ENABLED", "false")
            ),
            media_video_provider=os.getenv(
                "OMF_MEDIA_VIDEO_PROVIDER", "comfyui"
            ).strip(),
            media_image_provider=os.getenv(
                "OMF_MEDIA_IMAGE_PROVIDER", "comfyui"
            ).strip(),
            comfyui_base_url=os.getenv(
                "COMFYUI_BASE_URL", "http://host.docker.internal:8188"
            ).rstrip("/"),
            comfyui_output_dir=Path(
                os.getenv(
                    "COMFYUI_OUTPUT_DIR", str(data_dir / "inbox/generated/comfyui")
                )
            ).expanduser().resolve(),
            comfyui_image_workflow=Path(
                os.getenv(
                    "COMFYUI_IMAGE_WORKFLOW", "/app/config/workflows/image.json"
                )
            ).expanduser().resolve(),
            comfyui_video_workflow=Path(
                os.getenv(
                    "COMFYUI_VIDEO_WORKFLOW", "/app/config/workflows/video.json"
                )
            ).expanduser().resolve(),
            comfyui_video_i2v_workflow=Path(
                os.getenv(
                    "COMFYUI_VIDEO_I2V_WORKFLOW",
                    "/app/config/workflows/video_i2v.json",
                )
            ).expanduser().resolve(),
            local_media_runtime_base_url=os.getenv(
                "LOCAL_MEDIA_RUNTIME_BASE_URL", "http://host.docker.internal:8090"
            ).rstrip("/"),
            local_media_runtime_api_key=(
                os.getenv("LOCAL_MEDIA_RUNTIME_API_KEY", "")
                or os.getenv("OMF_API_KEY", "change-me")
            ),
            lip_sync_enabled=_as_bool(os.getenv("OMF_LIP_SYNC_ENABLED", "false")),
            lip_sync_base_url=os.getenv(
                "OMF_LIP_SYNC_BASE_URL", "http://host.docker.internal:8090"
            ).rstrip("/"),
            lip_sync_api_key=(
                os.getenv("OMF_LIP_SYNC_API_KEY", "")
                or os.getenv("LOCAL_MEDIA_RUNTIME_API_KEY", "")
                or os.getenv("OMF_API_KEY", "change-me")
            ),
            lip_sync_fallback_to_narration=_as_bool(
                os.getenv("OMF_LIP_SYNC_FALLBACK_TO_NARRATION", "true")
            ),
            lip_sync_min_score=min(
                1.0, max(0.0, float(os.getenv("OMF_LIP_SYNC_MIN_SCORE", "0.65")))
            ),
            lip_sync_min_face_coverage=min(
                1.0,
                max(
                    0.0,
                    float(os.getenv("OMF_LIP_SYNC_MIN_FACE_COVERAGE", "0.80")),
                ),
            ),
            frame_interpolation_enabled=_as_bool(
                os.getenv("OMF_FRAME_INTERPOLATION_ENABLED", "true")
            ),
            frame_interpolation_multiplier=min(
                4,
                max(2, int(os.getenv("OMF_FRAME_INTERPOLATION_MULTIPLIER", "2"))),
            ),
        )


settings = Settings.from_env()
