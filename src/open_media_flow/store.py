from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Protocol

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from .models import Automation, AutomationRun, ContentTask, utc_now


class TaskNotFoundError(KeyError):
    pass


class AutomationNotFoundError(KeyError):
    pass


class TaskStore(Protocol):
    def create(self, task: ContentTask) -> ContentTask: ...
    def list(self) -> list[ContentTask]: ...
    def get(self, task_id: str) -> ContentTask: ...
    def save(self, task: ContentTask) -> ContentTask: ...


class JsonTaskStore:
    """Lightweight store used by unit tests and optional non-Compose development."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _read_all(self) -> dict[str, ContentTask]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {task_id: ContentTask.model_validate(value) for task_id, value in raw.items()}

    def _write_all(self, tasks: dict[str, ContentTask]) -> None:
        temporary = self.path.with_suffix(".tmp")
        payload = {key: value.model_dump(mode="json") for key, value in tasks.items()}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def create(self, task: ContentTask) -> ContentTask:
        with self._lock:
            tasks = self._read_all()
            tasks[task.id] = task
            self._write_all(tasks)
        return task

    def list(self) -> list[ContentTask]:
        with self._lock:
            return sorted(
                self._read_all().values(), key=lambda item: item.created_at, reverse=True
            )

    def get(self, task_id: str) -> ContentTask:
        with self._lock:
            task = self._read_all().get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def save(self, task: ContentTask) -> ContentTask:
        with self._lock:
            tasks = self._read_all()
            if task.id not in tasks:
                raise TaskNotFoundError(task.id)
            task.updated_at = utc_now()
            tasks[task.id] = task
            self._write_all(tasks)
        return task


metadata = MetaData()
tasks_table = Table(
    "content_tasks",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
automations_table = Table(
    "automations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
automation_runs_table = Table(
    "automation_runs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("automation_id", String(64), nullable=False, index=True),
    Column("task_id", String(64), nullable=False, index=True),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class PostgresStore:
    """PostgreSQL-backed task, automation, and run history store."""

    def __init__(self, database_url: str):
        self.engine: Engine = create_engine(database_url, pool_pre_ping=True)
        metadata.create_all(self.engine)

    def create(self, task: ContentTask) -> ContentTask:
        payload = task.model_dump(mode="json")
        with self.engine.begin() as connection:
            connection.execute(
                insert(tasks_table).values(
                    id=task.id,
                    payload=payload,
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                )
            )
        return task

    def list(self) -> list[ContentTask]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(tasks_table.c.payload).order_by(tasks_table.c.created_at.desc())
            )
            return [ContentTask.model_validate(row.payload) for row in rows]

    def get(self, task_id: str) -> ContentTask:
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(tasks_table.c.payload).where(tasks_table.c.id == task_id)
            ).scalar_one_or_none()
        if payload is None:
            raise TaskNotFoundError(task_id)
        return ContentTask.model_validate(payload)

    def save(self, task: ContentTask) -> ContentTask:
        task.updated_at = utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(tasks_table)
                .where(tasks_table.c.id == task.id)
                .values(payload=task.model_dump(mode="json"), updated_at=task.updated_at)
            )
        if result.rowcount == 0:
            raise TaskNotFoundError(task.id)
        return task

    def create_automation(self, automation: Automation) -> Automation:
        with self.engine.begin() as connection:
            connection.execute(
                insert(automations_table).values(
                    id=automation.id,
                    payload=automation.model_dump(mode="json"),
                    created_at=automation.created_at,
                    updated_at=automation.updated_at,
                )
            )
        return automation

    def list_automations(self) -> list[Automation]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(automations_table.c.payload).order_by(
                    automations_table.c.created_at.desc()
                )
            )
            return [Automation.model_validate(row.payload) for row in rows]

    def get_automation(self, automation_id: str) -> Automation:
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(automations_table.c.payload).where(
                    automations_table.c.id == automation_id
                )
            ).scalar_one_or_none()
        if payload is None:
            raise AutomationNotFoundError(automation_id)
        return Automation.model_validate(payload)

    def save_automation(self, automation: Automation) -> Automation:
        automation.updated_at = utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(automations_table)
                .where(automations_table.c.id == automation.id)
                .values(
                    payload=automation.model_dump(mode="json"),
                    updated_at=automation.updated_at,
                )
            )
        if result.rowcount == 0:
            raise AutomationNotFoundError(automation.id)
        return automation

    def create_run(self, run: AutomationRun) -> AutomationRun:
        with self.engine.begin() as connection:
            connection.execute(
                insert(automation_runs_table).values(
                    id=run.id,
                    automation_id=run.automation_id,
                    task_id=run.task_id,
                    payload=run.model_dump(mode="json"),
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                )
            )
        return run

    def save_run(self, run: AutomationRun) -> AutomationRun:
        run.updated_at = utc_now()
        with self.engine.begin() as connection:
            connection.execute(
                update(automation_runs_table)
                .where(automation_runs_table.c.id == run.id)
                .values(payload=run.model_dump(mode="json"), updated_at=run.updated_at)
            )
        return run

    def get_run(self, run_id: str) -> AutomationRun | None:
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(automation_runs_table.c.payload).where(
                    automation_runs_table.c.id == run_id
                )
            ).scalar_one_or_none()
        return AutomationRun.model_validate(payload) if payload else None

    def list_runs(self, automation_id: str | None = None) -> list[AutomationRun]:
        statement = select(automation_runs_table.c.payload)
        if automation_id:
            statement = statement.where(
                automation_runs_table.c.automation_id == automation_id
            )
        statement = statement.order_by(automation_runs_table.c.created_at.desc())
        with self.engine.connect() as connection:
            rows = connection.execute(statement)
            return [AutomationRun.model_validate(row.payload) for row in rows]

    def delete_automation(self, automation_id: str) -> None:
        with self.engine.begin() as connection:
            result = connection.execute(
                delete(automations_table).where(automations_table.c.id == automation_id)
            )
        if result.rowcount == 0:
            raise AutomationNotFoundError(automation_id)
