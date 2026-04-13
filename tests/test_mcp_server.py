from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hub.mcp_server import (
    hub_list_tasks,
    hub_propose_task,
    hub_report_done,
    hub_start_task,
    hub_task_status,
    hub_task_update,
)


@pytest.fixture
def mock_api_get() -> AsyncMock:
    with patch("hub.mcp_server._api_get", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_api_post() -> AsyncMock:
    with patch("hub.mcp_server._api_post", new_callable=AsyncMock) as m:
        yield m


async def test_hub_list_tasks(mock_api_get: AsyncMock) -> None:
    mock_api_get.return_value = [
        {
            "id": 1,
            "status": "open",
            "runtime": "auto",
            "title": "Alpha",
            "task_type": "task",
        },
        {
            "id": 2,
            "status": "running",
            "runtime": "vast",
            "title": "Beta epic",
            "task_type": "epic",
            "source": "agent",
            "assigned_agent": "coder",
        },
        {
            "id": 3,
            "status": "open",
            "runtime": "auto",
            "title": "Child",
            "task_type": "subtask",
            "parent_id": 2,
        },
    ]
    out = await hub_list_tasks()
    lines = out.split("\n")
    assert lines[0] == "#1 [open] (auto) Alpha"
    assert lines[1] == "#2 [epic] [running] (vast) [agent:coder] Beta epic"
    assert lines[2] == "#3 [subtask] [open] (auto) (parent #2) Child"
    mock_api_get.assert_awaited_once_with("/api/tasks?limit=20")


async def test_hub_task_detail(mock_api_get: AsyncMock, mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {}
    mock_api_get.return_value = {
        "id": 42,
        "title": "Inspect me",
        "status": "running",
        "source": "human",
        "runtime": "auto",
        "assigned_agent": "tester",
        "job_id": "job-9",
        "exit_code": None,
        "auto_review": True,
        "review_cycle": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updates": [
            {
                "created_at": "2026-01-02T00:00:00Z",
                "kind": "status",
                "agent": "a1",
                "content": "Started",
            },
        ],
        "result_text": "",
        "log_tail": ["line1", "line2"],
    }
    out = await hub_task_status(42)
    assert "Task #42: Inspect me" in out
    assert "Status: running" in out
    assert "Agent: tester" in out
    assert "Job ID: job-9" in out
    assert "[2026-01-02T00:00:00Z] (status) a1: Started" in out
    assert "Log tail:" in out and "line1" in out and "line2" in out
    mock_api_post.assert_awaited_once_with("/api/tasks/42/refresh")
    mock_api_get.assert_awaited_once_with("/api/tasks/42")


async def test_hub_propose(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"id": 100}
    msg = await hub_propose_task(
        "New thing",
        "Do the thing",
        agent="architect",
        rationale="Because",
        parent_id=7,
    )
    assert "Draft task #100 created" in msg
    mock_api_post.assert_awaited_once_with(
        "/api/tasks",
        {
            "title": "New thing",
            "description": "Do the thing",
            "source": "agent",
            "agent": "architect",
            "rationale": "Because",
            "parent_id": 7,
        },
    )


async def test_hub_start_task(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"status": "running", "job_id": "dispatch-1"}
    msg = await hub_start_task(5, plan="Step one then two", runtime="openrouter")
    assert "Task #5 dispatched" in msg
    assert "dispatch-1" in msg
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/5/start",
        {"plan": "Step one then two", "runtime": "openrouter"},
    )


async def test_hub_update(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"id": 55}
    msg = await hub_task_update(4, "Plan: ship it", agent="dev", kind="status")
    assert "Update #55 added to task #4" in msg
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/4/updates",
        {"agent": "dev", "kind": "status", "content": "Plan: ship it"},
    )


async def test_hub_report_done(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"id": 77}
    msg = await hub_report_done(
        9,
        "Changed: tests. Validation: pytest -q",
        agent="qa",
    )
    assert "Done report #77 submitted for task #9" in msg
    call_args = mock_api_post.await_args
    assert call_args is not None
    assert call_args.args[0] == "/api/tasks/9/updates"
    assert call_args.args[1] == {
        "agent": "qa",
        "kind": "done",
        "content": "Changed: tests. Validation: pytest -q",
    }
