"""T15 acceptance: read_task list, five-level URL parsing and the read-only guarantee.

The level flags are parsed from the raw multi-value query (a dict would hide duplicates), and
every call is compared against row snapshots before/after so "查看不占用" is proven, not
assumed.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from apps.audit.models import ActivityEvent
from apps.identity.models import Agent
from apps.jobs.models import IdempotencyRecord, OutboxEvent
from apps.projects.models import Project
from apps.tasks.models import Task, TaskClaimLock, TaskExecution
from domain.contracts import ClaimMode, ProjectStatus, TaskStatus
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.claim]

READ_TASK = "/api/agent/v1/read_task/"
AGENT_TASKS = "/api/agent/v1/tasks"
WEB_TASKS = "/api/web/v1/tasks"

REQUIRED_ITEM_FIELDS = {
    "id",
    "project_id",
    "number",
    "display_number",
    "title",
    "description",
    "acceptance_criteria",
    "difficulty",
    "difficulty_name",
    "status",
    "claim_mode",
    "designated_agent_id",
    "assignee_agent_id",
    "version",
    "execution_timeout_seconds",
    "claimable_for_me",
}


@pytest.fixture
def agent_api():
    from fastapi.testclient import TestClient

    from agentapi.main import app

    return TestClient(app)


def _get(api, path: str, token: str):
    return api.get(path, headers={"AGENT_TOKEN": token})


class Scenario:
    """Project A (S1/W1/W2) plus project B (U/S1) with one task per interesting shape."""

    def __init__(self, world) -> None:
        self.world = world
        self.levels = {}
        for difficulty in (1, 2, 3, 4, 5):
            self.levels[difficulty] = factories.create_task(
                world.project_a,
                title=f"等级 {difficulty} 任务",
                difficulty=difficulty,
                status=TaskStatus.TODO,
                claim_mode=ClaimMode.OPEN,
                description=f"L{difficulty} 描述",
                acceptance_criteria=f"L{difficulty} 验收",
            )
        self.preparing = factories.create_task(
            world.project_a, title="准备中任务", status=TaskStatus.PREPARING
        )
        self.archived = factories.create_task(
            world.project_a, title="已归档任务", status=TaskStatus.TODO, is_archived=True
        )
        self.assigned_to_w1 = factories.create_task(
            world.project_a,
            title="指定给 W1",
            status=TaskStatus.TODO,
            claim_mode=ClaimMode.ASSIGNED,
            designated_agent=world.w1,
        )
        self.assigned_to_w2 = factories.create_task(
            world.project_a,
            title="指定给 W2",
            status=TaskStatus.TODO,
            claim_mode=ClaimMode.ASSIGNED,
            designated_agent=world.w2,
        )
        self.project_b_task = factories.create_task(
            world.project_b, title="B 项目任务", status=TaskStatus.TODO
        )

    def visible_todo_ids(self, project_ids) -> set:
        return set(
            Task.objects.filter(
                project_id__in=project_ids, status=TaskStatus.TODO.value, is_archived=False
            ).values_list("pk", flat=True)
        )


@pytest.mark.django_db(transaction=True)
def test_read_task_defaults_to_todo_all_levels_and_only_member_projects(world, agent_api):
    scenario = Scenario(world)
    response = _get(agent_api, READ_TASK, world.w1_key)
    assert response.status_code == 200, response.content
    body = response.json()
    data = body["data"]
    assert body["request_id"]
    assert response.headers["X-Request-Id"] == body["request_id"]

    assert data["selected_levels"] == ["L1", "L2", "L3", "L4", "L5"]
    assert data["available_capacity"] == 1
    assert data["page"] == 1
    assert data["page_size"] == 20

    expected = scenario.visible_todo_ids({world.project_a.pk})
    assert set(item["id"] for item in data["items"]) == {str(pk) for pk in expected}
    assert data["total"] == len(expected)

    by_id = {item["id"]: item for item in data["items"]}
    for difficulty, level_task in scenario.levels.items():
        item = by_id[str(level_task.pk)]
        assert REQUIRED_ITEM_FIELDS <= set(item)
        assert item["difficulty"] == difficulty
        assert item["difficulty_name"] == f"L{difficulty}"
        assert item["execution_timeout_seconds"] == {
            1: 900,
            2: 1800,
            3: 7200,
            4: 28800,
            5: 86400,
        }[difficulty]
        assert item["status"] == TaskStatus.TODO.value
        assert item["display_number"].startswith("TASK-")
        assert item["description"] == f"L{difficulty} 描述"
        assert item["acceptance_criteria"] == f"L{difficulty} 验收"
        assert item["claimable_for_me"] is True
        assert item["claim_mode"] == ClaimMode.OPEN.value
        assert item["assignee_agent_id"] is None
        assert item["version"] == level_task.version

    # PREPARING, archived and other-project tasks are absent by default.
    ids = {item["id"] for item in data["items"]}
    assert str(scenario.preparing.pk) not in ids
    assert str(scenario.archived.pk) not in ids
    assert str(scenario.project_b_task.pk) not in ids

    # W1 may claim the OPEN tasks and the one designated to it, but not W2's.
    claimable = {item["id"]: item["claimable_for_me"] for item in data["items"]}
    assert claimable[str(scenario.assigned_to_w1.pk)] is True
    assert claimable[str(scenario.assigned_to_w2.pk)] is False


@pytest.mark.django_db(transaction=True)
def test_read_task_status_filter_and_archived_visibility(world, agent_api):
    scenario = Scenario(world)

    preparing = _get(agent_api, f"{READ_TASK}?status=PREPARING", world.w1_key)
    assert preparing.status_code == 200, preparing.content
    assert preparing.json()["data"]["total"] == 0
    assert preparing.json()["data"]["items"] == []

    supervisor_preparing = _get(agent_api, f"{READ_TASK}?status=PREPARING", world.s1_key)
    assert supervisor_preparing.status_code == 200
    ids = {item["id"] for item in supervisor_preparing.json()["data"]["items"]}
    assert ids == {str(scenario.preparing.pk)}

    archived = _get(agent_api, f"{READ_TASK}?include_archived=true", world.w1_key)
    assert archived.status_code == 200
    assert str(scenario.archived.pk) in {item["id"] for item in archived.json()["data"]["items"]}

    invalid = _get(agent_api, f"{READ_TASK}?status=CLAIMED", world.w1_key)
    assert invalid.status_code == 400, invalid.content
    assert invalid.json()["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.django_db(transaction=True)
def test_level_flags_select_exactly_the_requested_levels_for_both_roles(world, agent_api):
    scenario = Scenario(world)

    for token in (world.w1_key, world.s1_key):
        both = _get(agent_api, f"{READ_TASK}?l1=1&l2=1", token)
        assert both.status_code == 200, both.content
        data = both.json()["data"]
        assert data["selected_levels"] == ["L1", "L2"]
        assert {item["difficulty"] for item in data["items"]} == {1, 2}
        # S1 also belongs to project B, so the expected row set differs per identity while
        # the level semantics stay identical.
        project_scope = (
            {world.project_a.pk, world.project_b.pk}
            if token == world.s1_key
            else {world.project_a.pk}
        )
        assert data["total"] == Task.objects.filter(
            project_id__in=project_scope,
            status=TaskStatus.TODO.value,
            is_archived=False,
            difficulty__in=[1, 2],
        ).count()

        only_five = _get(agent_api, f"{READ_TASK}?l5=1", token)
        assert only_five.status_code == 200
        assert only_five.json()["data"]["selected_levels"] == ["L5"]
        assert [item["difficulty"] for item in only_five.json()["data"]["items"]] == [5]

        excluded = _get(agent_api, f"{READ_TASK}?l1=0&l2=1", token)
        assert excluded.status_code == 200
        assert excluded.json()["data"]["selected_levels"] == ["L2"]

        explicit_all = _get(
            agent_api, f"{READ_TASK}?l1=1&l2=1&l3=1&l4=1&l5=1", token
        )
        assert explicit_all.json()["data"]["selected_levels"] == [
            "L1",
            "L2",
            "L3",
            "L4",
            "L5",
        ]

        none_selected = _get(
            agent_api, f"{READ_TASK}?l1=0&l2=0&l3=0&l4=0&l5=0", token
        )
        assert none_selected.status_code == 200, none_selected.content
        payload = none_selected.json()["data"]
        assert payload["items"] == []
        assert payload["total"] == 0
        assert payload["selected_levels"] == []
        # Still a valid list response with the capacity hint.
        assert payload["available_capacity"] == (
            1 if token == world.w1_key else 5
        )

    # The default (no lN at all) includes every level for both roles.
    for token in (world.w1_key, world.s1_key):
        default = _get(agent_api, READ_TASK, token)
        assert default.json()["data"]["selected_levels"] == [
            "L1",
            "L2",
            "L3",
            "L4",
            "L5",
        ]
        assert {item["difficulty"] for item in default.json()["data"]["items"]} == {
            1,
            2,
            3,
            4,
            5,
        }

    assert Task.objects.get(pk=scenario.levels[1].pk).status == TaskStatus.TODO.value


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "query",
    [
        "l0=1",
        "l6=1",
        "l1=1&l1=0",
        "l1=2",
        "l1=",
        "l1=1&difficulty=2",
        "l1=1&unknown=1",
        "status=TODO&status=DONE",
        "available=maybe",
        "page=0",
        "page_size=101",
        "project_id=not-a-uuid",
    ],
)
def test_read_task_rejects_illegal_parameters_without_touching_rows(world, agent_api, query):
    scenario = Scenario(world)
    before = _snapshot()

    response = _get(agent_api, f"{READ_TASK}?{query}", world.w1_key)
    assert response.status_code == 400, (query, response.content)
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert _snapshot() == before
    assert Task.objects.filter(pk=scenario.levels[3].pk).exists()


@pytest.mark.django_db(transaction=True)
def test_available_true_filters_claim_conditions_but_never_hides_on_full_capacity(
    world, agent_api
):
    scenario = Scenario(world)
    Agent.objects.filter(pk=world.w1.pk).update(reserved_execution_count=1)
    world.w1.refresh_from_db()

    full = _get(agent_api, f"{READ_TASK}?available=true", world.w1_key)
    assert full.status_code == 200, full.content
    data = full.json()["data"]
    assert data["available_capacity"] == 0
    ids = {item["id"] for item in data["items"]}
    # Every still-claimable task shape stays visible even with no free slot.
    for level_task in scenario.levels.values():
        assert str(level_task.pk) in ids
    assert str(scenario.assigned_to_w1.pk) in ids
    assert str(scenario.assigned_to_w2.pk) not in ids
    assert str(scenario.archived.pk) not in ids
    assert str(scenario.project_b_task.pk) not in ids
    assert all(item["claimable_for_me"] is False for item in data["items"])

    Agent.objects.filter(pk=world.w1.pk).update(reserved_execution_count=0)
    free = _get(agent_api, f"{READ_TASK}?available=true&l1=1&l2=1", world.w1_key)
    assert free.status_code == 200
    assert free.json()["data"]["available_capacity"] == 1
    assert free.json()["data"]["selected_levels"] == ["L1", "L2"]
    assert all(item["claimable_for_me"] is True for item in free.json()["data"]["items"])

    conflict = _get(agent_api, f"{READ_TASK}?available=true&status=DONE", world.w1_key)
    assert conflict.status_code == 400
    assert conflict.json()["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.django_db(transaction=True)
def test_read_task_orders_by_sort_order_and_paginates_stably(world, agent_api):
    # sort_order deliberately reversed against creation order to prove ordering is real.
    created = [
        factories.create_task(
            world.project_a,
            title=f"排序 {index}",
            difficulty=3,
            sort_order=Decimal(100_000 - index * 1_000),
        )
        for index in range(25)
    ]
    assert len(created) == 25

    first = _get(agent_api, f"{READ_TASK}?l3=1", world.w1_key)
    assert first.status_code == 200, first.content
    data = first.json()["data"]
    assert data["total"] == 25
    assert len(data["items"]) == 20
    # sort_order ascends while creation order descends, so this order is not incidental.
    ordered_titles = [f"排序 {24 - index}" for index in range(25)]
    assert [item["title"] for item in data["items"]] == ordered_titles[:20]

    second = _get(agent_api, f"{READ_TASK}?l3=1&page=2", world.w1_key)
    assert second.status_code == 200
    assert [item["title"] for item in second.json()["data"]["items"]] == ordered_titles[20:]

    page_one_ids = {item["id"] for item in data["items"]}
    page_two_ids = {item["id"] for item in second.json()["data"]["items"]}
    assert not (page_one_ids & page_two_ids)
    assert page_one_ids | page_two_ids == {str(task.pk) for task in created}

    sized = _get(agent_api, f"{READ_TASK}?l3=1&page_size=100", world.w1_key)
    assert sized.status_code == 200
    assert len(sized.json()["data"]["items"]) == 25

    too_big = _get(agent_api, f"{READ_TASK}?l3=1&page_size=101", world.w1_key)
    assert too_big.status_code == 400

    # The database ordering and the API ordering agree (sort_order, created_at, id).
    expected = list(
        Task.objects.filter(project_id=world.project_a.pk, difficulty=3)
        .order_by("sort_order", "created_at", "id")
        .values_list("pk", flat=True)
    )
    all_items = first.json()["data"]["items"] + second.json()["data"]["items"]
    assert [item["id"] for item in all_items] == [str(pk) for pk in expected]


@pytest.mark.django_db(transaction=True)
def test_read_task_never_changes_tasks_executions_capacity_or_history(world, agent_api):
    scenario = Scenario(world)
    active_task = scenario.levels[2]
    agent = world.w1
    Agent.objects.filter(pk=agent.pk).update(reserved_execution_count=1)
    from domain.clock import db_now

    now = db_now()
    execution = factories.create_execution(active_task, agent, claimed_at=now)
    active_task.status = TaskStatus.IN_PROGRESS.value
    active_task.assignee_agent = agent
    active_task.active_execution = execution
    active_task.claimed_at = now
    active_task.version = 2
    active_task.save(
        update_fields=["status", "assignee_agent", "active_execution", "claimed_at", "version"]
    )
    ActivityEvent.objects.create(
        project=world.project_a, task=active_task, actor_type="system", action="claimed"
    )

    before = _snapshot()

    calls = [
        READ_TASK,
        f"{READ_TASK}?available=true",
        f"{READ_TASK}?l1=1&l2=1&page=1&page_size=5",
        f"{READ_TASK}?l1=0&l2=0&l3=0&l4=0&l5=0",
        f"{READ_TASK}?include_archived=true&status=IN_PROGRESS",
        f"{READ_TASK}?project_id={world.project_a.pk}",
        f"{READ_TASK}?q=等级",
        f"{READ_TASK}?l9=1",
        f"{READ_TASK}?page_size=0",
    ]
    for query in calls:
        response = _get(agent_api, query, world.w1_key)
        assert response.status_code in (200, 400), (query, response.content)

    assert _snapshot() == before
    task = Task.objects.get(pk=active_task.pk)
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.active_execution_id == execution.pk
    assert task.version == 2
    execution.refresh_from_db()
    assert execution.ended_at is None
    assert Agent.objects.get(pk=agent.pk).reserved_execution_count == 1
    assert Task.objects.filter(pk=scenario.archived.pk).get().is_archived is True


@pytest.mark.django_db(transaction=True)
def test_read_task_is_scoped_to_the_agents_projects(world, agent_api):
    scenario = Scenario(world)

    outsider = _get(agent_api, READ_TASK, world.u_key)
    assert outsider.status_code == 200, outsider.content
    assert [item["id"] for item in outsider.json()["data"]["items"]] == [
        str(scenario.project_b_task.pk)
    ]

    project_a_task = _get(agent_api, f"{AGENT_TASKS}/{scenario.levels[1].pk}", world.u_key)
    assert project_a_task.status_code == 404
    assert project_a_task.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    missing_project = _get(
        agent_api, f"{READ_TASK}?project_id={world.project_a.pk}", world.u_key
    )
    assert missing_project.status_code == 200
    assert missing_project.json()["data"]["items"] == []


@pytest.mark.django_db(transaction=True)
def test_read_task_and_detail_require_a_valid_agent_token(world, agent_api):
    Scenario(world)
    assert agent_api.get(READ_TASK).status_code == 401
    assert agent_api.get(READ_TASK).json()["error"]["code"] == "AGENT_TOKEN_MISSING"

    bogus = _get(agent_api, READ_TASK, "bogus-key")
    assert bogus.status_code == 401
    assert bogus.json()["error"]["code"] == "AGENT_TOKEN_INVALID"


@pytest.mark.django_db(transaction=True)
def test_worker_cannot_read_a_preparing_task_but_supervisor_can(world, agent_api):
    scenario = Scenario(world)
    url = f"{AGENT_TASKS}/{scenario.preparing.pk}"

    worker = _get(agent_api, url, world.w1_key)
    assert worker.status_code == 404, worker.content
    assert worker.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    supervisor = _get(agent_api, url, world.s1_key)
    assert supervisor.status_code == 200, supervisor.content
    assert supervisor.json()["data"]["status"] == TaskStatus.PREPARING.value
    assert supervisor.json()["data"]["description"] == "描述"

    # A TODO task is readable by the worker.
    readable = _get(agent_api, f"{AGENT_TASKS}/{scenario.levels[1].pk}", world.w1_key)
    assert readable.status_code == 200
    assert readable.json()["data"]["number"] == scenario.levels[1].number


@pytest.mark.django_db(transaction=True)
def test_agentapi_task_activity_is_member_scoped(world, agent_api):
    scenario = Scenario(world)
    ActivityEvent.objects.create(
        project=world.project_a,
        task=scenario.levels[1],
        actor_type="web_operator",
        action="created",
    )
    response = _get(agent_api, f"{AGENT_TASKS}/{scenario.levels[1].pk}/activity", world.w1_key)
    assert response.status_code == 200, response.content
    assert [item["action"] for item in response.json()["data"]["items"]] == ["created"]

    forbidden = _get(
        agent_api, f"{AGENT_TASKS}/{scenario.levels[1].pk}/activity", world.u_key
    )
    assert forbidden.status_code == 404


# --------------------------------------------------------------------------------------
# WebAPI: the integer difficulty filter is a different parameter from the lN flags
# --------------------------------------------------------------------------------------


def test_webapi_difficulty_filter_is_an_integer_and_rejects_the_agent_level_flags(client, world):
    scenario = Scenario(world)

    response = client.get(f"{WEB_TASKS}?difficulty=3")
    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert {item["difficulty"] for item in data["items"]} == {3}
    assert "selected_levels" not in data
    assert "claimable_for_me" not in data["items"][0]

    # The WebAPI list sees PREPARING too and does not hide archived tasks when asked.
    preparing = client.get(f"{WEB_TASKS}?status=PREPARING")
    assert preparing.status_code == 200
    assert {item["id"] for item in preparing.json()["data"]["items"]} == {
        str(scenario.preparing.pk)
    }

    archived = client.get(f"{WEB_TASKS}?include_archived=true&status=TODO")
    assert archived.status_code == 200
    assert str(scenario.archived.pk) in {
        item["id"] for item in archived.json()["data"]["items"]
    }

    for query, code in (
        ("difficulty=0", "INVALID_ARGUMENT"),
        ("difficulty=6", "INVALID_ARGUMENT"),
        ("difficulty=abc", "INVALID_ARGUMENT"),
        ("l1=1", "UNSUPPORTED_PARAMETER"),
        ("status=TODO&status=DONE", "INVALID_ARGUMENT"),
        ("unknown=1", "UNSUPPORTED_PARAMETER"),
    ):
        bad = client.get(f"{WEB_TASKS}?{query}")
        assert bad.status_code == 400, (query, bad.content)
        assert bad.json()["error"]["code"] == code, (query, bad.content)

    # The project scope is part of the query, not a client-side filter.
    project_b = client.get(f"{WEB_TASKS}?project_id={world.project_b.pk}")
    assert project_b.status_code == 200
    assert {item["id"] for item in project_b.json()["data"]["items"]} == {
        str(scenario.project_b_task.pk)
    }


def test_webapi_list_defaults_to_todo_and_matches_the_database_order(client, world):
    scenario = Scenario(world)
    response = client.get(f"{WEB_TASKS}?page_size=100")
    assert response.status_code == 200, response.content
    data = response.json()["data"]

    expected = list(
        Task.objects.filter(
            project_id__in=[world.project_a.pk, world.project_b.pk],
            status=TaskStatus.TODO.value,
            is_archived=False,
        )
        .order_by("sort_order", "created_at", "id")
        .values_list("pk", flat=True)
    )
    assert data["total"] == len(expected)
    assert [item["id"] for item in data["items"]] == [str(pk) for pk in expected]
    assert str(scenario.preparing.pk) not in {item["id"] for item in data["items"]}

    unknown_task = client.get(f"{WEB_TASKS}/{uuid.uuid4()}")
    assert unknown_task.status_code == 404
    assert unknown_task.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


# --------------------------------------------------------------------------------------


def _snapshot() -> dict:
    """Every row a read must not change (T15 acceptance: 只读前后数据比较)."""
    return {
        "tasks": list(
            Task.objects.order_by("pk").values_list(
                "pk", "status", "version", "requirements_version", "updated_at",
                "is_archived", "assignee_agent_id", "active_execution_id", "claimed_at",
            )
        ),
        "executions": list(
            TaskExecution.objects.order_by("pk").values_list(
                "pk", "ended_at", "end_reason", "progress_percent", "progress_updated_at"
            )
        ),
        "agents": list(
            Agent.objects.order_by("pk").values_list(
                "pk", "reserved_execution_count", "last_heartbeat_at", "updated_at"
            )
        ),
        "activity": ActivityEvent.objects.count(),
        "outbox": OutboxEvent.objects.count(),
        "receipts": IdempotencyRecord.objects.count(),
        "claim_lock": list(TaskClaimLock.objects.order_by("pk").values_list("pk", "revision")),
        "projects": list(
            Project.objects.order_by("pk").values_list("pk", "status", "version")
        ),
    }
