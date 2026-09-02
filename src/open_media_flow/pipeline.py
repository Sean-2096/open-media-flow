from __future__ import annotations

from .audit import ContentAuditor
from .models import ContentTask, TaskStatus
from .publishers.base import Publisher
from .store import TaskStore


class Pipeline:
    def __init__(self, store: TaskStore, auditor: ContentAuditor, publisher: Publisher):
        self.store = store
        self.auditor = auditor
        self.publisher = publisher

    def audit(self, task: ContentTask) -> ContentTask:
        task.audit = self.auditor.review(task)
        task.status = TaskStatus.APPROVED if task.audit.approved else TaskStatus.REVIEW_REJECTED
        return self.store.save(task)

    def publish(self, task: ContentTask) -> ContentTask:
        if not task.audit or not task.audit.approved:
            raise ValueError("task must pass review before publishing")
        task.status = TaskStatus.PUBLISHING
        self.store.save(task)
        task.publish_results = [self.publisher.publish(task, platform) for platform in task.platforms]
        successes = [result.success for result in task.publish_results]
        task.status = TaskStatus.PUBLISHED if all(successes) else TaskStatus.PARTIAL_FAILURE
        return self.store.save(task)
