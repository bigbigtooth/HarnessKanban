"""Reusable test data builders.

Kept deliberately small and explicit: tests assert on database rows, not on HTTP status
codes alone, so builders return the model instances they create.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from apps.identity.models import Agent
from apps.projects.models import Project, ProjectMember
from apps.tasks.models import Task, TaskClaimLock, TaskExecution
from domain.contracts import AgentRole, ClaimMode, DIFFICULTY_TIMEOUT_SECONDS, ProjectStatus, TaskStatus
from domain.security import generate_agent_key, hash_agent_key


def create_agent(
    name: str,
    role: str = AgentRole.WORKER,
    *,
    max_concurrency: int = 1,
    enabled: bool = True,
    key_version: int = 1,
    reserved_execution_count: int = 0,
    raw_key: str | None = None,
) -> tuple[Agent, str]:
    key = raw_key or generate_agent_key()
    agent = Agent.objects.create(
        name=name,
        role=role.value if isinstance(role, AgentRole) else role,
        enabled=enabled,
        key_hash=hash_agent_key(key),
        key_version=key_version,
        max_concurrency=max_concurrency,
        reserved_execution_count=reserved_execution_count,
    )
    return agent, key


def create_project(
    title: str,
    *,
    status: str = ProjectStatus.ACTIVE,
    background: str = "",
    archived_from_status: str | None = None,
) -> Project:
    from apps.projects.models import ProjectContextVersion

    project = Project.objects.create(
        title=title,
        status=status.value if isinstance(status, ProjectStatus) else status,
        background=background,
        archived_from_status=archived_from_status,
    )
    ProjectContextVersion.objects.create(
        project=project,
        number=project.content_version,
        background_snapshot=background,
        document_version_refs=[],
    )
    return project


def add_member(project: Project, agent: Agent) -> ProjectMember:
    return ProjectMember.objects.create(project=project, agent=agent)


def create_task(
    project: Project,
    *,
    title: str = "任务",
    number: int | None = None,
    difficulty: int = 2,
    status: str = TaskStatus.TODO,
    claim_mode: str = ClaimMode.OPEN,
    designated_agent: Agent | None = None,
    assignee_agent: Agent | None = None,
    active_execution: TaskExecution | None = None,
    current_submission=None,
    description: str = "描述",
    acceptance_criteria: str = "验收标准",
    sort_order: Decimal | None = None,
    due_at=None,
    is_archived: bool = False,
) -> Task:
    status_value = status.value if isinstance(status, TaskStatus) else status
    if number is None:
        last = (
            Task.objects.filter(project=project).order_by("-number").values_list("number", flat=True).first()
        )
        number = (last or 0) + 1
    return Task.objects.create(
        project=project,
        number=number,
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        difficulty=difficulty,
        status=status_value,
        claim_mode=claim_mode.value if isinstance(claim_mode, ClaimMode) else claim_mode,
        designated_agent=designated_agent,
        assignee_agent=assignee_agent,
        active_execution=active_execution,
        current_submission=current_submission,
        sort_order=sort_order if sort_order is not None else Decimal(number) * Decimal("1000"),
        due_at=due_at,
        is_archived=is_archived,
    )


def create_execution(
    task: Task,
    agent: Agent,
    *,
    sequence: int = 1,
    claimed_at,
    timeout_seconds: int | None = None,
    difficulty: int | None = None,
    ended_at=None,
    end_reason: str | None = None,
) -> TaskExecution:
    """Create an execution row directly (test-only shortcut for deadline boundary cases)."""
    difficulty_value = difficulty if difficulty is not None else task.difficulty
    timeout = timeout_seconds if timeout_seconds is not None else DIFFICULTY_TIMEOUT_SECONDS[difficulty_value]
    return TaskExecution.objects.create(
        task=task,
        agent=agent,
        sequence=sequence,
        difficulty_snapshot=difficulty_value,
        execution_timeout_seconds=timeout,
        requirements_snapshot={
            "title": task.title,
            "description": task.description,
            "acceptance_criteria": task.acceptance_criteria,
            "difficulty": difficulty_value,
            "requirements_version": task.requirements_version,
        },
        claimed_at=claimed_at,
        execution_deadline_at=claimed_at + timedelta(seconds=timeout),
        ended_at=ended_at,
        end_reason=end_reason,
    )


class World:
    """The D20 public acceptance fixture: S1/S2 supervisors, W1/W2 workers, U outsider."""

    def __init__(self) -> None:
        self.s1, self.s1_key = create_agent("S1", AgentRole.SUPERVISOR, max_concurrency=5)
        self.s2, self.s2_key = create_agent("S2", AgentRole.SUPERVISOR, max_concurrency=5)
        self.w1, self.w1_key = create_agent("W1", AgentRole.WORKER, max_concurrency=1)
        self.w2, self.w2_key = create_agent("W2", AgentRole.WORKER, max_concurrency=2)
        self.u, self.u_key = create_agent("U", AgentRole.WORKER, max_concurrency=1)

        self.project_a = create_project("项目 A", status=ProjectStatus.ACTIVE, background="A 背景")
        self.project_b = create_project("项目 B", status=ProjectStatus.ACTIVE, background="B 背景")
        self.archived_project = create_project(
            "归档项目",
            status=ProjectStatus.ARCHIVED,
            archived_from_status=ProjectStatus.ACTIVE.value,
        )

        for agent in (self.s1, self.s2, self.w1, self.w2):
            add_member(self.project_a, agent)
        add_member(self.project_b, self.u)
        add_member(self.project_b, self.s1)

    def agents(self) -> list[Agent]:
        return [self.s1, self.s2, self.w1, self.w2, self.u]


def ensure_claim_lock() -> TaskClaimLock:
    from apps.tasks.claim_lock import ensure_claim_lock_row

    ensure_claim_lock_row()
    return TaskClaimLock.objects.get(id=1)


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
