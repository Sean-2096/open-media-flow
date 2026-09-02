from __future__ import annotations

from typing import Protocol

from ..models import ContentTask, Platform, PublishResult


class Publisher(Protocol):
    def publish(self, task: ContentTask, platform: Platform) -> PublishResult: ...

