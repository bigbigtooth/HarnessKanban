"""T24 acceptance: in-column ordering with real-neighbour validation.

Ordering is verified to be a pure reorder: the task's column, execution, submission and
deadline must all be untouched, and a stale or filtered view must not be able to reshuffle
cards the caller never saw.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest

from apps.tasks.models import Task, TaskExecution
from domain.claim_service import ClaimRequest, claim_task
from domain.contracts import ActorContext, ActorType, ClaimMode, TaskStatus
from domain.errors import (
    InvalidArgumentError,
    InvalidTransitionError,
    ResourceNotFoundError,
    VersionConflictError,
)
from domain.ordering_service import MIN_SORT_GAP, reorder_task
from domain.service_runner import run_request_service
from tests import factories
from tests.conftest import agent_actor

pytestmark = [pytest.mark.workflow, pytest.mark.api]

HOST = {"HTTP_HOST": "127.0.0.1:8000"}
ORDER_URL = "/api/web/v1/projects/{project_id}/task-order"


@pytest.fixture
def client():
    from django.test import Client

    return Client()


def new_key() -> str:
    return str(uuid.uuid4())


def order(client, project, payload):
    return client.post(
        ORDER_URL.format(project_id=project.pk),
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=new_key(),
        **HOST,
    )


def body(response):
    return json.loads(response.content)


def column_order(project, status=TaskStatus.TODO.value):
    return list(
        Task.objects.filter(project=project, status=status, is_archived=False)
        .order_by("sort_order", "created_at", "id")
        .values_list("title", flat=True)
    )


def three_todo_tasks(world):
    return [
        factories.create_task(
            world.project_a,
            title=f"卡片{index}",
            difficulty=1,
            status=TaskStatus.TODO,
            sort_order=Decimal(index) * Decimal("1000"),
        )
        for index in (1, 2, 3)
    ]


def test_moving_a_card_to_the_top_places_it_first(db, client):
    world = factories.World()
    first, second, third = three_todo_tasks(world)
    assert column_order(world.project_a) == ["卡片1", "卡片2", "卡片3"]

    response = order(
        client,
        world.project_a,
        {"task_id": str(third.pk), "after_task_id": str(first.pk), "expected_version": third.version},
    )

    assert response.status_code == 200, response.content
    assert body(response)["data"]["sort_order"] == str((first.sort_order - Decimal("1000")).quantize(Decimal("0.0000000001")))
    assert column_order(world.project_a) == ["卡片3", "卡片1", "卡片2"]
    third.refresh_from_db()
    assert third.version == 2


def test_moving_a_card_between_two_anchors_uses_the_midpoint(db, client):
    world = factories.World()
    first, second, third = three_todo_tasks(world)

    response = order(
        client,
        world.project_a,
        {"task_id": str(third.pk), "before_task_id": str(first.pk), "after_task_id": str(second.pk), "expected_version": third.version},
    )

    assert response.status_code == 200, response.content
    assert column_order(world.project_a) == ["卡片1", "卡片3", "卡片2"]


def test_reordering_never_touches_column_execution_or_submission(db, client):
    world = factories.World()
    held, other = three_todo_tasks(world)[:2]
    # Give the moved task a real live execution first.
    claimed = run_request_service(
        lambda: claim_task(agent_actor(world.w1), ClaimRequest(levels=[1]), new_key()),
        global_claim_lock=True,
    )
    task = Task.objects.get(pk=claimed["data"]["task_id"])
    execution = TaskExecution.objects.get(pk=claimed["data"]["execution_id"])
    task.refresh_from_db()
    before = {
        "status": task.status,
        "assignee": task.assignee_agent_id,
        "execution": task.active_execution_id,
        "deadline": execution.execution_deadline_at,
        "requirements_version": task.requirements_version,
    }
    assert before["status"] == TaskStatus.IN_PROGRESS.value

    # Reorder inside the IN_PROGRESS column: its only neighbour is itself, so use the
    # explicit one-anchor form against a second in-progress card.
    second_holder, _ = factories.create_agent("T24-W2", "WORKER", max_concurrency=1)
    factories.add_member(world.project_a, second_holder)
    other_task = factories.create_task(world.project_a, difficulty=1, status=TaskStatus.TODO)
    claimed_two = run_request_service(
        lambda: claim_task(
            agent_actor(second_holder), ClaimRequest(levels=[1], task_id=other_task.pk, expected_version=other_task.version), new_key()
        ),
        global_claim_lock=True,
    )
    other_task.refresh_from_db()
    assert other_task.status == TaskStatus.IN_PROGRESS.value

    response = order(
        client,
        world.project_a,
        {
            "task_id": str(task.pk),
            "before_task_id": str(other_task.pk),
            "expected_version": task.version,
        },
    )
    assert response.status_code == 200, response.content

    task.refresh_from_db()
    execution.refresh_from_db()
    assert task.status == before["status"]
    assert task.assignee_agent_id == before["assignee"]
    assert task.active_execution_id == before["execution"]
    assert task.requirements_version == before["requirements_version"]
    assert execution.execution_deadline_at == before["deadline"]
    assert execution.ended_at is None


def test_neighbours_in_another_column_are_refused(db, client):
    world = factories.World()
    todo, _ = three_todo_tasks(world)[:2]
    preparing = factories.create_task(
        world.project_a, title="准备中卡片", difficulty=1, status=TaskStatus.PREPARING
    )

    response = order(
        client,
        world.project_a,
        {"task_id": str(todo.pk), "before_task_id": str(preparing.pk), "expected_version": todo.version},
    )

    assert response.status_code == 409
    assert body(response)["error"]["code"] == "INVALID_TRANSITION"
    todo.refresh_from_db()
    assert todo.status == TaskStatus.TODO.value


def test_asserted_column_must_match_the_task_status(db, client):
    """A drag across columns must not be smuggled through the ordering endpoint."""
    world = factories.World()
    todo, _ = three_todo_tasks(world)[:2]

    response = order(
        client,
        world.project_a,
        {
            "task_id": str(todo.pk),
            "before_task_id": str(world.project_a.tasks.exclude(pk=todo.pk).first().pk),
            "status": TaskStatus.DONE.value,
            "expected_version": todo.version,
        },
    )

    assert response.status_code == 409
    assert body(response)["error"]["code"] == "INVALID_TRANSITION"
    todo.refresh_from_db()
    assert todo.status == TaskStatus.TODO.value


def test_non_adjacent_anchors_are_refused_so_a_filtered_view_cannot_reshuffle_cards(db, client):
    world = factories.World()
    first, second, third = three_todo_tasks(world)

    # A filtered board would believe 卡片1 and 卡片3 are adjacent; 卡片2 sits between them.
    response = order(
        client,
        world.project_a,
        {"task_id": str(second.pk), "before_task_id": str(third.pk), "after_task_id": str(first.pk), "expected_version": second.version},
    )

    assert response.status_code in (400, 409)
    second.refresh_from_db()
    assert second.sort_order == Decimal("2000")
    assert column_order(world.project_a) == ["卡片1", "卡片2", "卡片3"]


def test_cross_project_anchor_is_refused(db, client):
    world = factories.World()
    todo, _ = three_todo_tasks(world)[:2]
    foreign = factories.create_task(world.project_b, title="别的项目", difficulty=1, status=TaskStatus.TODO)

    response = order(
        client,
        world.project_a,
        {"task_id": str(todo.pk), "before_task_id": str(foreign.pk), "expected_version": todo.version},
    )

    assert response.status_code == 400
    assert body(response)["error"]["code"] == "INVALID_ARGUMENT"


def test_stale_version_is_a_conflict_and_does_not_overwrite(db, client):
    world = factories.World()
    first, second, third = three_todo_tasks(world)
    stale_version = third.version

    first_move = order(
        client,
        world.project_a,
        {"task_id": str(third.pk), "after_task_id": str(first.pk), "expected_version": stale_version},
    )
    assert first_move.status_code == 200, first_move.content

    # A second client that still holds the old version must not reshuffle anything.
    second_client_move = order(
        client,
        world.project_a,
        {"task_id": str(third.pk), "after_task_id": str(second.pk), "expected_version": stale_version},
    )
    assert second_client_move.status_code == 409
    assert body(second_client_move)["error"]["code"] == "VERSION_CONFLICT"
    assert column_order(world.project_a) == ["卡片3", "卡片1", "卡片2"]


def test_exhausted_gap_triggers_a_column_renumber(db, client):
    world = factories.World()
    dense = MIN_SORT_GAP / 4
    first = factories.create_task(
        world.project_a, title="密集1", difficulty=1, sort_order=Decimal("1000")
    )
    second = factories.create_task(
        world.project_a, title="密集2", difficulty=1, sort_order=Decimal("1000") + dense
    )
    mover = factories.create_task(
        world.project_a, title="密集3", difficulty=1, sort_order=Decimal("3000")
    )

    response = order(
        client,
        world.project_a,
        {"task_id": str(mover.pk), "before_task_id": str(first.pk), "after_task_id": str(second.pk), "expected_version": mover.version},
    )

    assert response.status_code == 200, response.content
    assert body(response)["data"]["renumbered"] is True
    assert column_order(world.project_a) == ["密集1", "密集3", "密集2"]
    # Renumbering re-spaces the whole column with a fixed step.
    values = list(
        Task.objects.filter(project=world.project_a, status=TaskStatus.TODO.value, is_archived=False)
        .order_by("sort_order")
        .values_list("sort_order", flat=True)
    )
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_archived_task_cannot_be_ordered(db, client):
    world = factories.World()
    task = factories.create_task(
        world.project_a, title="归档卡片", difficulty=1, is_archived=True
    )
    anchor = factories.create_task(world.project_a, title="正常卡片", difficulty=1)

    response = order(
        client,
        world.project_a,
        {"task_id": str(task.pk), "before_task_id": str(anchor.pk), "expected_version": task.version},
    )

    assert response.status_code == 409
    assert body(response)["error"]["code"] == "INVALID_TRANSITION"


def test_reorder_requires_an_anchor_and_an_idempotency_key(db, client):
    world = factories.World()
    task = factories.create_task(world.project_a, difficulty=1)

    no_anchor = order(
        client, world.project_a, {"task_id": str(task.pk), "expected_version": task.version}
    )
    assert no_anchor.status_code == 400
    assert body(no_anchor)["error"]["code"] == "INVALID_ARGUMENT"

    missing_key = client.post(
        ORDER_URL.format(project_id=world.project_a.pk),
        data=json.dumps({"task_id": str(task.pk), "expected_version": task.version}),
        content_type="application/json",
        **HOST,
    )
    assert missing_key.status_code == 400
    assert body(missing_key)["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_replay_returns_the_same_order_without_a_second_change(db, client):
    world = factories.World()
    first, second, third = three_todo_tasks(world)
    key = new_key()
    payload = {"task_id": str(third.pk), "after_task_id": str(first.pk), "expected_version": third.version}

    first_response = client.post(
        ORDER_URL.format(project_id=world.project_a.pk),
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key,
        **HOST,
    )
    replay = client.post(
        ORDER_URL.format(project_id=world.project_a.pk),
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key,
        **HOST,
    )

    assert first_response.status_code == 200
    assert replay.status_code == 200
    assert body(replay)["data"]["idempotent_replay"] is True
    third.refresh_from_db()
    assert third.version == 2  # replayed, not incremented again
    assert column_order(world.project_a) == ["卡片3", "卡片1", "卡片2"]


def test_ordering_stays_stable_across_a_full_column_reorder(db, client):
    world = factories.World()
    tasks = [
        factories.create_task(
            world.project_a,
            title=f"稳定{index}",
            difficulty=1,
            sort_order=Decimal(index) * Decimal("1000"),
        )
        for index in range(1, 7)
    ]

    # Move the last card to the very front, one real move.
    response = order(
        client,
        world.project_a,
        {"task_id": str(tasks[-1].pk), "after_task_id": str(tasks[0].pk), "expected_version": tasks[-1].version},
    )
    assert response.status_code == 200, response.content

    order_titles = column_order(world.project_a)
    assert order_titles[0] == "稳定6"
    assert sorted(order_titles) == sorted(f"稳定{index}" for index in range(1, 7))
    assert len(order_titles) == len(set(order_titles))


@pytest.mark.mysql
@pytest.mark.concurrency
@pytest.mark.django_db(transaction=True)
def test_concurrent_ordering_lets_only_one_client_win(world):
    """Two clients reorder the same card from the same version: exactly one order survives."""
    import threading

    from django.db import connections

    first = factories.create_task(
        world.project_a, title="并发1", difficulty=1, sort_order=Decimal("1000")
    )
    second = factories.create_task(
        world.project_a, title="并发2", difficulty=1, sort_order=Decimal("2000")
    )
    mover = factories.create_task(
        world.project_a, title="并发3", difficulty=1, sort_order=Decimal("3000")
    )
    expected_version = mover.version
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    guard = threading.Lock()

    def worker(field: str, anchor) -> None:
        try:
            barrier.wait(timeout=20)
            run_request_service(
                lambda: reorder_task(
                    ActorContext(actor_type=ActorType.WEB_OPERATOR, request_id=str(uuid.uuid4())),
                    world.project_a.pk,
                    payload={
                        "task_id": str(mover.pk),
                        field: str(anchor.pk),
                        "expected_version": expected_version,
                    },
                    idem_key=str(uuid.uuid4()),
                ),
                require_write_lock=False,
            )
            with guard:
                outcomes.append(field)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            with guard:
                outcomes.append(type(exc).__name__)
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=worker, args=("after_task_id", first)),
        threading.Thread(target=worker, args=("before_task_id", second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert outcomes.count("after_task_id") + outcomes.count("before_task_id") == 1, outcomes
    assert "VersionConflictError" in outcomes, outcomes
    mover.refresh_from_db()
    assert mover.version == expected_version + 1
    # Only one client's decision is reflected; the other never silently overwrote it.
    titles = column_order(world.project_a)
    assert titles[0] == "并发3" or titles[-1] == "并发3"
    assert len(titles) == 3
