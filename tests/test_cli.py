from __future__ import annotations

import argparse
import json
from io import StringIO
from unittest.mock import MagicMock, patch

from hub import cli


def test_cmd_list() -> None:
    tasks = [
        {
            "id": 1,
            "title": "Alpha",
            "status": "open",
            "task_type": "task",
            "runtime": "auto",
            "source": "human",
        },
    ]
    mock_api = MagicMock(return_value=tasks)
    args = argparse.Namespace(limit=10, status="open", type=None, parent=None)
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()) as out:
        rc = cli.cmd_list(args)
    assert rc == 0
    mock_api.assert_called_once_with("GET", "/api/tasks?limit=10&status=open")
    assert "#1" in out.getvalue()
    assert "Alpha" in out.getvalue()


def test_cmd_create() -> None:
    created = {"id": 99, "title": "New task", "status": "open"}
    mock_api = MagicMock(return_value=created)
    args = argparse.Namespace(
        title="New task",
        description="Desc",
        runtime="vast",
        run=True,
        no_review=True,
        parent=5,
        task_type="task",
        priority="high",
    )
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()) as out:
        rc = cli.cmd_task(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks",
        {
            "title": "New task",
            "description": "Desc",
            "runtime": "vast",
            "source": "human",
            "run_immediately": True,
            "auto_review": False,
            "task_type": "task",
            "priority": "high",
            "parent_id": 5,
        },
    )
    assert json.loads(out.getvalue()) == created


def test_cmd_start() -> None:
    result = {"id": 3, "status": "running"}
    mock_api = MagicMock(return_value=result)
    args = argparse.Namespace(task_id=3, plan="Step one", runtime="openrouter")
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_start(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/3/start",
        {"plan": "Step one", "runtime": "openrouter"},
    )


def test_cmd_update() -> None:
    upd = {"id": 1, "kind": "status", "content": "Done X"}
    mock_api = MagicMock(return_value=upd)
    args = argparse.Namespace(task_id=12, agent="tester", kind="blocker", message="Blocked by CI")
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()):
        rc = cli.cmd_update(args)
    assert rc == 0
    mock_api.assert_called_once_with(
        "POST",
        "/api/tasks/12/updates",
        {"agent": "tester", "kind": "blocker", "content": "Blocked by CI"},
    )


def test_cmd_show() -> None:
    task = {"id": 7, "title": "Show me", "status": "running"}
    mock_api = MagicMock(return_value=task)
    args = argparse.Namespace(task_id=7)
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()) as out:
        rc = cli.cmd_status(args)
    assert rc == 0
    mock_api.assert_called_once_with("GET", "/api/tasks/7")
    assert json.loads(out.getvalue()) == task


def test_cmd_tree() -> None:
    tree = {
        "id": 1,
        "title": "Root",
        "task_type": "epic",
        "status": "open",
        "progress": {"completed": 1, "total": 4, "percent": 25},
        "children": [
            {
                "id": 2,
                "title": "Child",
                "task_type": "task",
                "status": "open",
                "children": [],
            },
        ],
    }
    mock_api = MagicMock(return_value=tree)
    args = argparse.Namespace(task_id=1)
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=StringIO()) as out:
        rc = cli.cmd_tree(args)
    assert rc == 0
    mock_api.assert_called_once_with("GET", "/api/tasks/1/tree")
    text = out.getvalue()
    assert "[epic] #1 Root" in text
    assert "  [task] #2 Child" in text
