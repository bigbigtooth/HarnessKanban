"""Task five-state model, executions, submissions, reviews and the singleton claim lock.

Column-local invariants from DEVELOP D01 are expressed as portable ``CheckConstraint``s so
an invalid combination cannot be committed even if a future service forgets a check.
Cross-table rules (execution belongs to task and assignee, review belongs to a valid
supervisor, ...) remain domain-transaction checks.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models

from domain.contracts import ClaimMode, EndReason, ReviewDecision, TaskStatus

TASK_STATUS_CHOICES = [(status.value, status.value) for status in TaskStatus]
CLAIM_MODE_CHOICES = [(mode.value, mode.value) for mode in ClaimMode]
REVIEW_DECISION_CHOICES = [(decision.value, decision.value) for decision in ReviewDecision]
END_REASON_CHOICES = [(reason.value, reason.value) for reason in EndReason]

SORT_ORDER_STEP = Decimal("1000")
SORT_ORDER_MIN_GAP = Decimal("0.000001")


class TaskClaimLock(models.Model):
    """Singleton row (id=1) serialising every claim entry point.

    Inserted by the data migration. ``flush`` truncates that row; ``post_migrate``
    and a rolled-back lock attempt both put id=1 back without resetting revision.
    The business transaction still refuses to continue unless ``UPDATE`` locks that row.
    """

    id = models.PositiveSmallIntegerField(primary_key=True)
    revision = models.BigIntegerField(default=0)

    class Meta:
        db_table = "tasks_claim_lock"


class Task(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project", on_delete=models.PROTECT, related_name="tasks"
    )
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    acceptance_criteria = models.TextField(blank=True, default="")
    difficulty = models.PositiveSmallIntegerField()
    status = models.CharField(
        max_length=20, choices=TASK_STATUS_CHOICES, default=TaskStatus.PREPARING.value
    )
    claim_mode = models.CharField(
        max_length=20, choices=CLAIM_MODE_CHOICES, default=ClaimMode.OPEN.value
    )
    designated_agent = models.ForeignKey(
        "identity.Agent", on_delete=models.PROTECT, null=True, blank=True, related_name="designated_tasks"
    )
    assignee_agent = models.ForeignKey(
        "identity.Agent", on_delete=models.PROTECT, null=True, blank=True, related_name="assigned_tasks"
    )
    active_execution = models.OneToOneField(
        "tasks.TaskExecution", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    current_submission = models.OneToOneField(
        "tasks.TaskSubmission", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    sort_order = models.DecimalField(max_digits=30, decimal_places=10, default=SORT_ORDER_STEP)
    due_at = models.DateTimeField(null=True, blank=True)

    version = models.PositiveIntegerField(default=1)
    requirements_version = models.PositiveIntegerField(default=1)
    is_archived = models.BooleanField(default=False)
    labels = models.JSONField(default=list, blank=True)

    created_by_agent = models.ForeignKey(
        "identity.Agent", on_delete=models.PROTECT, null=True, blank=True, related_name="created_tasks"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "tasks_task"
        constraints = [
            models.UniqueConstraint(fields=["project", "number"], name="uniq_task_project_number"),
            models.CheckConstraint(
                condition=models.Q(difficulty__gte=1) & models.Q(difficulty__lte=5),
                name="ck_task_difficulty_range",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=[status.value for status in TaskStatus]),
                name="ck_task_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(claim_mode__in=[mode.value for mode in ClaimMode]),
                name="ck_task_claim_mode_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(claim_mode=ClaimMode.ASSIGNED.value, designated_agent__isnull=False)
                    | models.Q(claim_mode=ClaimMode.OPEN.value, designated_agent__isnull=True)
                ),
                name="ck_task_designated_matches_mode",
            ),
            # PREPARING / TODO keep no execution, no submission and no assignee.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=[TaskStatus.PREPARING.value, TaskStatus.TODO.value])
                    | (
                        models.Q(active_execution__isnull=True)
                        & models.Q(current_submission__isnull=True)
                        & models.Q(assignee_agent__isnull=True)
                    )
                ),
                name="ck_task_idle_states_empty_refs",
            ),
            # IN_PROGRESS has a current execution and assignee, and no current submission.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status=TaskStatus.IN_PROGRESS.value)
                    | (
                        models.Q(active_execution__isnull=False)
                        & models.Q(assignee_agent__isnull=False)
                        & models.Q(current_submission__isnull=True)
                    )
                ),
                name="ck_task_in_progress_shape",
            ),
            # IN_REVIEW / DONE have no effective execution and keep a current submission.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=[TaskStatus.IN_REVIEW.value, TaskStatus.DONE.value])
                    | (
                        models.Q(active_execution__isnull=True)
                        & models.Q(current_submission__isnull=False)
                        & models.Q(assignee_agent__isnull=False)
                    )
                ),
                name="ck_task_finished_states_have_submission",
            ),
            models.CheckConstraint(
                condition=models.Q(number__gte=1),
                name="ck_task_number_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "status"], name="ix_task_project_status"),
            models.Index(fields=["status", "claim_mode", "designated_agent"], name="ix_task_claim_pool"),
            models.Index(fields=["project", "difficulty"], name="ix_task_project_difficulty"),
            models.Index(fields=["assignee_agent", "status"], name="ix_task_assignee_status"),
            models.Index(fields=["due_at"], name="ix_task_due_at"),
            models.Index(
                fields=["project", "status", "is_archived", "sort_order"],
                name="ix_task_board_order",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - debug helper
        return f"TASK-{self.number:04d} {self.title}"


class TaskExecution(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="executions")
    agent = models.ForeignKey(
        "identity.Agent", on_delete=models.PROTECT, related_name="executions"
    )
    sequence = models.PositiveIntegerField()
    difficulty_snapshot = models.PositiveSmallIntegerField()
    execution_timeout_seconds = models.PositiveIntegerField()
    requirements_snapshot = models.JSONField(default=dict, blank=True)
    context_version = models.ForeignKey(
        "projects.ProjectContextVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="executions",
    )
    claimed_at = models.DateTimeField()
    execution_deadline_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=20, choices=END_REASON_CHOICES, null=True, blank=True)
    progress_message = models.TextField(blank=True, default="")
    progress_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    progress_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tasks_execution"
        constraints = [
            models.UniqueConstraint(fields=["task", "sequence"], name="uniq_execution_task_sequence"),
            models.CheckConstraint(
                condition=models.Q(difficulty_snapshot__gte=1) & models.Q(difficulty_snapshot__lte=5),
                name="ck_execution_difficulty_range",
            ),
            models.CheckConstraint(
                condition=models.Q(execution_timeout_seconds__gte=1),
                name="ck_execution_timeout_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(execution_deadline_at__gt=models.F("claimed_at")),
                name="ck_execution_deadline_after_claim",
            ),
            models.CheckConstraint(
                condition=models.Q(progress_percent__isnull=True) | (
                    models.Q(progress_percent__gte=0) & models.Q(progress_percent__lte=100)
                ),
                name="ck_execution_progress_percent_range",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(ended_at__isnull=True, end_reason__isnull=True)
                    | models.Q(ended_at__isnull=False, end_reason__isnull=False)
                ),
                name="ck_execution_end_pair",
            ),
        ]
        indexes = [
            models.Index(fields=["ended_at"], name="ix_execution_ended_at"),
            models.Index(fields=["execution_deadline_at"], name="ix_execution_deadline"),
            models.Index(fields=["agent", "ended_at"], name="ix_execution_agent_ended"),
        ]
        ordering = ["task", "sequence"]

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


class TaskSubmission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    execution = models.OneToOneField(
        TaskExecution, on_delete=models.PROTECT, related_name="submission"
    )
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="submissions")
    summary = models.CharField(max_length=200)
    content = models.TextField(blank=True, default="")
    self_check = models.JSONField(default=list, blank=True)
    artifact_refs = models.JSONField(default=list, blank=True)
    links = models.JSONField(default=list, blank=True)
    requirements_version = models.PositiveIntegerField()
    context_version = models.ForeignKey(
        "projects.ProjectContextVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="submissions",
    )
    submitted_by_agent = models.ForeignKey(
        "identity.Agent", on_delete=models.PROTECT, related_name="submissions"
    )
    submitted_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tasks_submission"
        indexes = [
            models.Index(fields=["task", "submitted_at"], name="ix_submission_task_time"),
        ]
        ordering = ["task", "submitted_at"]


class TaskReview(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.OneToOneField(
        TaskSubmission, on_delete=models.PROTECT, related_name="review"
    )
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="reviews")
    decision = models.CharField(max_length=20, choices=REVIEW_DECISION_CHOICES)
    comment = models.TextField(blank=True, default="")
    actor_type = models.CharField(max_length=20)
    reviewer_agent = models.ForeignKey(
        "identity.Agent", on_delete=models.PROTECT, null=True, blank=True, related_name="reviews"
    )
    reviewer_identity_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tasks_review"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(decision__in=[decision.value for decision in ReviewDecision]),
                name="ck_review_decision_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["task", "created_at"], name="ix_review_task_time"),
        ]
