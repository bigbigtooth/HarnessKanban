"""Fixed lock order and lock-plan validation (DEVELOP D05/D12).

Global claim lock (when required) -> projects -> agents -> tasks -> executions, with rows
of the same kind locked in ascending id order. Multi-object operations must decide the
whole id set first and lock by category, never walk the full order once per record.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from django.db.models import Model

from apps.identity.models import Agent
from apps.projects.models import Project
from apps.tasks.models import Task, TaskExecution
from domain.errors import RetryTransaction
from domain.transactions import is_sqlite

logger = logging.getLogger("domain.locking")


@dataclass
class LockPlan:
    """The complete set of related rows an operation intends to touch."""

    project_ids: set[uuid.UUID] = field(default_factory=set)
    agent_ids: set[uuid.UUID] = field(default_factory=set)
    task_ids: set[uuid.UUID] = field(default_factory=set)
    execution_ids: set[uuid.UUID] = field(default_factory=set)

    def add_project(self, value: uuid.UUID | None) -> None:
        if value:
            self.project_ids.add(value)

    def add_agent(self, value: uuid.UUID | None) -> None:
        if value:
            self.agent_ids.add(value)

    def add_task(self, value: uuid.UUID | None) -> None:
        if value:
            self.task_ids.add(value)

    def add_execution(self, value: uuid.UUID | None) -> None:
        if value:
            self.execution_ids.add(value)

    def merge(self, other: "LockPlan") -> None:
        self.project_ids |= other.project_ids
        self.agent_ids |= other.agent_ids
        self.task_ids |= other.task_ids
        self.execution_ids |= other.execution_ids

    def contains_task(self, value: uuid.UUID | None) -> bool:
        return value is None or value in self.task_ids

    def contains_agent(self, value: uuid.UUID | None) -> bool:
        return value is None or value in self.agent_ids

    def contains_project(self, value: uuid.UUID | None) -> bool:
        return value is None or value in self.project_ids

    def contains_execution(self, value: uuid.UUID | None) -> bool:
        return value is None or value in self.execution_ids


@dataclass
class LockedRows:
    projects: dict[uuid.UUID, Project] = field(default_factory=dict)
    agents: dict[uuid.UUID, Agent] = field(default_factory=dict)
    tasks: dict[uuid.UUID, Task] = field(default_factory=dict)
    executions: dict[uuid.UUID, TaskExecution] = field(default_factory=dict)


def lock_and_load(model: type[Model], ids: set[uuid.UUID], *, using: str = "default") -> dict:
    """Lock the requested rows in ascending id order and return them keyed by id.

    On MySQL this issues ``SELECT ... FOR UPDATE``. SQLite has no row locks: writers are
    already serialised by the singleton write-lock row taken at transaction start, so the
    re-read here only refreshes rows inside that exclusive write transaction.
    """
    if not ids:
        return {}
    rows = (
        model.objects.using(using)
        .select_for_update()
        .filter(pk__in=sorted(ids))
        .order_by("pk")
    )
    return {row.pk: row for row in rows}


def acquire_plan(plan: LockPlan, *, using: str = "default") -> LockedRows:
    """Acquire every locked row of the plan in the fixed category order."""
    if is_sqlite(using):
        logger.debug("SQLite：写锁已由单例行取得，按同一顺序重读锁计划对象")
    return LockedRows(
        projects=lock_and_load(Project, plan.project_ids, using=using),
        agents=lock_and_load(Agent, plan.agent_ids, using=using),
        tasks=lock_and_load(Task, plan.task_ids, using=using),
        executions=lock_and_load(TaskExecution, plan.execution_ids, using=using),
    )


def require(condition: bool, message: str, *, details: dict | None = None) -> None:
    """Abort the whole transaction when a lock-plan precondition no longer holds.

    Raises ``RetryTransaction`` so the caller rolls back and re-plans, instead of holding
    a task lock while trying to acquire an out-of-plan agent/project lock.
    """
    if not condition:
        logger.info("锁计划失效，整笔回滚后重新规划：%s", message)
        raise RetryTransaction(message)
