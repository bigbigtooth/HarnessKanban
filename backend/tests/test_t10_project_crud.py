"""Project authoring acceptance plus the request-boundary envelope regressions.

Covers the WebAPI project list/create/detail/edit surface and the three boundary cases that
previously escaped the frozen envelope (bad Host, method not allowed, unknown path).
"""

from __future__ import annotations

import json
import uuid

import pytest

from apps.audit.models import ActivityEvent
from apps.projects.models import Project, ProjectContextVersion
from apps.tasks.models import Task
from domain.contracts import ProjectStatus
from tests import factories

pytestmark = [pytest.mark.api, pytest.mark.projects]

HOST = {"HTTP_HOST": "127.0.0.1:8000"}


@pytest.fixture
def client():
    from django.test import Client

    return Client()


def post_json(client, url, payload, *, key=None, **extra):
    headers = dict(HOST)
    headers.update(extra)
    return client.post(
        url,
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
        **headers,
    )


def patch_json(client, url, payload, *, key=None, **extra):
    headers = dict(HOST)
    headers.update(extra)
    return client.patch(
        url,
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
        **headers,
    )


def body(response):
    return json.loads(response.content)


def test_create_project_is_a_draft_with_an_initial_context_version(db, client):
    response = post_json(client, "/api/web/v1/projects", {"title": "新项目", "background": "背景 v1"})

    assert response.status_code == 201, response.content
    data = body(response)["data"]
    assert data["status"] == ProjectStatus.DRAFT.value
    assert data["content_version"] == 1
    assert data["version"] == 1

    project = Project.objects.get(pk=data["id"])
    assert project.title == "新项目"
    assert project.background == "背景 v1"
    versions = ProjectContextVersion.objects.filter(project=project)
    assert versions.count() == 1
    assert versions.first().number == 1
    assert versions.first().background_snapshot == "背景 v1"
    assert ActivityEvent.objects.filter(project=project, action="project.created").count() == 1


def test_create_project_replay_does_not_create_a_second_project(db, client):
    key = str(uuid.uuid4())
    first = post_json(client, "/api/web/v1/projects", {"title": "幂等项目"}, key=key)
    second = post_json(client, "/api/web/v1/projects", {"title": "幂等项目"}, key=key)

    assert first.status_code == 201
    assert second.status_code == 200
    assert body(second)["data"]["id"] == body(first)["data"]["id"]
    assert body(second)["data"]["idempotent_replay"] is True
    assert Project.objects.filter(title="幂等项目").count() == 1


def test_create_project_requires_title_and_idempotency_key(db, client):
    missing_title = post_json(client, "/api/web/v1/projects", {"summary": "只有简介"})
    assert missing_title.status_code == 400
    assert body(missing_title)["error"]["code"] == "INVALID_ARGUMENT"

    no_key = client.post(
        "/api/web/v1/projects",
        data=json.dumps({"title": "缺幂等键"}),
        content_type="application/json",
        **HOST,
    )
    assert no_key.status_code == 400
    assert body(no_key)["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert not Project.objects.filter(title="缺幂等键").exists()


def test_project_list_filters_and_hides_archived_on_request(db, client):
    world = factories.World()
    response = client.get("/api/web/v1/projects", **HOST)
    assert response.status_code == 200
    listed = body(response)["data"]
    assert listed["total"] >= 3

    without_archived = client.get("/api/web/v1/projects?include_archived=false", **HOST)
    ids = {item["id"] for item in body(without_archived)["data"]["items"]}
    assert str(world.archived_project.pk) not in ids

    active_only = client.get("/api/web/v1/projects?status=ACTIVE", **HOST)
    assert all(item["status"] == "ACTIVE" for item in body(active_only)["data"]["items"])

    bad_status = client.get("/api/web/v1/projects?status=NOPE", **HOST)
    assert bad_status.status_code == 400
    assert body(bad_status)["error"]["code"] == "INVALID_ARGUMENT"


def test_project_detail_reports_the_context_version_pointer(db, client):
    world = factories.World()
    response = client.get(f"/api/web/v1/projects/{world.project_a.pk}", **HOST)

    assert response.status_code == 200
    data = body(response)["data"]
    assert data["id"] == str(world.project_a.pk)
    assert data["current_context_version_number"] == world.project_a.content_version
    assert "background" in data


def test_metadata_edit_bumps_version_and_records_one_event(db, client):
    world = factories.World()
    version_before = world.project_a.version

    response = patch_json(
        client,
        f"/api/web/v1/projects/{world.project_a.pk}",
        {"title": "改名后的项目", "summary": "新简介", "expected_version": version_before},
    )

    assert response.status_code == 200, response.content
    world.project_a.refresh_from_db()
    assert world.project_a.title == "改名后的项目"
    assert world.project_a.summary == "新简介"
    assert world.project_a.version == version_before + 1
    assert (
        ActivityEvent.objects.filter(project=world.project_a, action="project.updated").count() == 1
    )
    # Metadata edits never touch the context version.
    assert ProjectContextVersion.objects.filter(project=world.project_a).count() == 1


def test_background_edit_advances_the_context_version_and_keeps_the_old_snapshot(db, client):
    world = factories.World()
    project = world.project_a
    version_before = project.version

    response = patch_json(
        client,
        f"/api/web/v1/projects/{project.pk}",
        {"background": "背景 v2", "expected_version": version_before},
    )

    assert response.status_code == 200, response.content
    project.refresh_from_db()
    assert project.background == "背景 v2"
    assert project.content_version == 2
    assert project.version == version_before + 1

    versions = ProjectContextVersion.objects.filter(project=project).order_by("number")
    assert [row.number for row in versions] == [1, 2]
    # V1 is immutable evidence and must still be readable after V2 exists.
    assert versions.first().background_snapshot != "背景 v2"


def test_background_cannot_be_mixed_with_other_fields_in_one_request(db, client):
    world = factories.World()
    response = patch_json(
        client,
        f"/api/web/v1/projects/{world.project_a.pk}",
        {"background": "背景", "title": "同时改名", "expected_version": world.project_a.version},
    )

    assert response.status_code == 400
    assert body(response)["error"]["code"] == "INVALID_ARGUMENT"
    world.project_a.refresh_from_db()
    assert world.project_a.title == "项目 A"


def test_stale_version_and_archived_project_are_rejected(db, client):
    world = factories.World()
    stale = patch_json(
        client,
        f"/api/web/v1/projects/{world.project_a.pk}",
        {"title": "过期版本", "expected_version": 99},
    )
    assert stale.status_code == 409
    assert body(stale)["error"]["code"] == "VERSION_CONFLICT"

    archived = patch_json(
        client,
        f"/api/web/v1/projects/{world.archived_project.pk}",
        {"title": "归档改名", "expected_version": world.archived_project.version},
    )
    assert archived.status_code == 409
    assert body(archived)["error"]["code"] == "INVALID_TRANSITION"
    world.archived_project.refresh_from_db()
    assert world.archived_project.title == "归档项目"


def test_forbidden_fields_are_rejected_on_project_edit(db, client):
    world = factories.World()
    response = patch_json(
        client,
        f"/api/web/v1/projects/{world.project_a.pk}",
        {"status": "ARCHIVED", "expected_version": world.project_a.version},
    )
    assert response.status_code == 400
    assert body(response)["error"]["code"] == "INVALID_ARGUMENT"
    world.project_a.refresh_from_db()
    assert world.project_a.status == ProjectStatus.ACTIVE.value


def test_bad_host_returns_the_frozen_403_envelope(db, client):
    """DisallowedHost must not bypass the documented REQUEST_ORIGIN_FORBIDDEN body."""
    response = client.get("/api/web/v1/projects", HTTP_HOST="evil.example.com")

    assert response.status_code == 403
    payload = body(response)
    assert payload["error"]["code"] == "REQUEST_ORIGIN_FORBIDDEN"
    assert payload["request_id"]
    assert "DisallowedHost" not in response.content.decode("utf-8")


def test_method_not_allowed_and_unknown_path_use_the_envelope(db, client):
    world = factories.World()

    wrong_method = client.get(f"/api/web/v1/projects/{world.project_a.pk}/activate", **HOST)
    assert wrong_method.status_code == 405
    assert body(wrong_method)["error"]["code"] == "METHOD_NOT_ALLOWED"

    unknown = client.get("/api/web/v1/definitely-not-a-route", **HOST)
    assert unknown.status_code == 404
    assert body(unknown)["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert "Traceback" not in unknown.content.decode("utf-8")
