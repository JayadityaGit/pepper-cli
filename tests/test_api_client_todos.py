"""Tests for the ApiClient todo methods.

Same ``httpx.MockTransport`` style as ``test_api_client.py`` — assert
the wire shape (method, URL, body) and that responses parse into
``TodoEntry`` dataclasses. This module is the contract the native
clients reproduce.
"""

from __future__ import annotations

import httpx

from pepper_cli import api_client
from pepper_cli.secret import SecretStr


def _mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _todo_json(**over) -> dict:
    base = {
        "id": "00000000-0000-0000-0000-000000000010",
        "title": "Buy milk",
        "details": None,
        "status": "open",
        "source": "user",
        "priority": None,
        "due_date": None,
        "created_at": "2026-06-05T00:00:00Z",
        "done_at": None,
        "source_ref": None,
    }
    base.update(over)
    return base


def _client(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    )


def test_list_todos_default_status_and_parse() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json=[_todo_json(priority="high")])

    with _client(handler) as api:
        rows = api.list_todos()

    assert seen["method"] == "GET"
    assert seen["url"] == "http://example.com/api/v1/todos?status=open"
    assert len(rows) == 1
    assert isinstance(rows[0], api_client.TodoEntry)
    assert rows[0].title == "Buy milk"
    assert rows[0].priority == "high"


def test_list_todos_passes_status() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[])

    with _client(handler) as api:
        assert api.list_todos(status="all") == []

    assert seen["url"] == "http://example.com/api/v1/todos?status=all"


def test_create_todo_posts_only_provided_fields() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_todo_json(title="Call dentist", priority="high"))

    with _client(handler) as api:
        todo = api.create_todo(title="Call dentist", priority="high")

    assert seen["method"] == "POST"
    assert seen["url"] == "http://example.com/api/v1/todos"
    # details / due_date omitted entirely when not provided
    assert seen["body"] == {"title": "Call dentist", "priority": "high"}
    assert todo.title == "Call dentist"


def test_create_todo_includes_due_and_details() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_todo_json())

    with _client(handler) as api:
        api.create_todo(title="x", details="more", due_date="2026-06-10T15:00:00-07:00")

    assert seen["body"] == {
        "title": "x",
        "details": "more",
        "due_date": "2026-06-10T15:00:00-07:00",
    }


def test_complete_and_dismiss_hit_right_urls() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        status = "done" if request.url.path.endswith("/done") else "dismissed"
        return httpx.Response(200, json=_todo_json(status=status))

    tid = "00000000-0000-0000-0000-000000000010"
    with _client(handler) as api:
        done = api.complete_todo(tid)
        dismissed = api.dismiss_todo(tid)

    assert seen == [
        ("POST", f"http://example.com/api/v1/todos/{tid}/done"),
        ("POST", f"http://example.com/api/v1/todos/{tid}/dismiss"),
    ]
    assert done.status == "done"
    assert dismissed.status == "dismissed"


def test_complete_todo_404_raises_api_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "todo not found"})

    with _client(handler) as api:
        try:
            api.complete_todo("00000000-0000-0000-0000-000000000099")
        except api_client.ApiError as exc:
            assert exc.status == 404
            assert exc.detail == "todo not found"
        else:  # pragma: no cover - failure path
            raise AssertionError("expected ApiError")


def test_room_todo_calls_use_the_typed_room_routes() -> None:
    seen: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode()))
        if request.method == "GET":
            return httpx.Response(200, json=[_todo_json()])
        return httpx.Response(200, json=_todo_json())

    c = _client(handler)
    assert [t.id for t in c.list_todos(status="all", group_id="g1")] == [_todo_json()["id"]]
    c.create_todo(title="x", due="2026-10-09", group_id="g1")
    c.complete_todo("t1", group_id="g1")
    c.reopen_todo("t1", group_id="g1")
    c.dismiss_todo("t1", group_id="g1")
    assert seen == [
        ("GET", "/api/v1/groups/g1/todos", "status=all"),
        ("POST", "/api/v1/groups/g1/todos", ""),
        ("POST", "/api/v1/groups/g1/todos/t1/done", ""),
        ("POST", "/api/v1/groups/g1/todos/t1/reopen", ""),
        ("POST", "/api/v1/groups/g1/todos/t1/dismiss", ""),
    ]
