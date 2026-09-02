from __future__ import annotations

from ..models import ContentTask, Platform, PublishResult


class DryRunPublisher:
    """Safe default: validate the handoff without touching any external account."""

    def publish(self, task: ContentTask, platform: Platform) -> PublishResult:
        return PublishResult(
            platform=platform,
            success=True,
            remote_id=f"dry-run:{task.id}:{platform.value}",
            detail="dry-run only; no external platform was changed",
        )

