"""OpenClaw Hub — FastAPI application with REST API and web dashboard."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from hub import config, services
from hub import db as db_module
from hub import repository as repo
from hub.db import get_db
from hub.integrations.registry import plugins
from hub.models import (
    ActivityItem,
    DashboardData,
    TaskAnswer,
    TaskApprove,
    TaskContextView,
    TaskCreate,
    TaskDecide,
    TaskQuestion,
    TaskReject,
    TaskReorder,
    TaskSource,
    TaskStart,
    TaskTreeNode,
    TaskUpdateCreate,
    TaskUpdateView,
    TaskView,
)
from hub.poller import start_poller
from hub.web import router as web_router

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
log = logging.getLogger("hub")

HERE = Path(__file__).parent


def _register_plugins() -> None:
    """Register concrete integration plugins based on available binaries/config."""
    from pathlib import Path

    if config.DISPATCH_BIN and Path(config.DISPATCH_BIN).exists():
        from hub.integrations.dispatch import DispatchIntegration

        plugins.dispatch = DispatchIntegration()

    if config.WORKSPACE_REPO_LINK and config.WORKSPACE_REPO_LINK.exists():
        from hub.integrations.git_ops import GitOpsIntegration

        plugins.git_ops = GitOpsIntegration()

    if config.GH_BIN:
        from hub.integrations.github import GitHubIntegration

        plugins.github = GitHubIntegration()

    if config.N4L_BIN:
        from hub.integrations.notes import NotesIntegration

        plugins.notes = NotesIntegration()

    if config.VAST_JOB_BIN:
        from hub.integrations.vast import VastIntegration

        plugins.vast = VastIntegration()

    if config.TRANSCRIPTS_DIR:
        from hub.integrations.transcripts import TranscriptsIntegration

        plugins.transcripts = TranscriptsIntegration()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _register_plugins()
    app.state.db = await get_db()
    log.info("Hub database ready at %s", config.HUB_DB_PATH)
    poll_task = start_poller(app)
    yield
    poll_task.cancel()
    await app.state.db.close()


app = FastAPI(title="OpenClaw Hub", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
app.include_router(web_router)


def _db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


# ---------------------------------------------------------------------------
# REST API — Tasks
# ---------------------------------------------------------------------------


@app.post("/api/tasks", response_model=TaskView)
async def api_create_task(body: TaskCreate, request: Request):
    return await services.create_task(_db(request), body)


@app.get("/api/tasks", response_model=list[TaskView])
async def api_list_tasks(
    request: Request,
    status: str | None = None,
    task_type: str | None = Query(default=None, alias="type"),
    priority: str | None = None,
    parent_id: int | None = None,
    limit: int = Query(default=50, le=200),
):
    return await services.list_tasks(
        _db(request),
        status=status,
        task_type=task_type,
        priority=priority,
        parent_id=parent_id,
        limit=limit,
    )


@app.get("/api/tasks/{task_id}", response_model=TaskView)
async def api_get_task(task_id: int, request: Request):
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    updates = await repo.get_task_updates(db, task_id)
    task_view = services.row_to_task(row, updates=updates)
    return await services.enrich_task_view(db, task_view)


# --- Hierarchy endpoints ---


@app.get("/api/tasks/{task_id}/tree", response_model=TaskTreeNode)
async def api_task_tree(task_id: int, request: Request):
    """Get recursive tree of a task and all descendants."""
    db = _db(request)
    tree = await db_module.build_tree(db, task_id)
    if not tree:
        raise HTTPException(404, "task not found")
    return tree


@app.get("/api/tasks/{task_id}/context", response_model=TaskContextView)
async def api_task_context(task_id: int, request: Request):
    """Get full context for an agent: breadcrumb, siblings, progress, n4l history."""
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)

    breadcrumb = await db_module.get_breadcrumb(db, task_id)
    children = await db_module.get_children(db, task_id)
    progress = await db_module.get_progress(db, task_id) if children else None

    siblings: list[dict[str, Any]] = []
    if task.get("parent_id"):
        sib_rows = await repo.get_siblings(db, task["parent_id"], task_id)
        siblings = [dict(r) for r in sib_rows]

    breadcrumb_str = " > ".join(
        f"{c['task_type'].capitalize()}: {c['title']} (#{c['id']})" for c in breadcrumb
    )

    context_lines = ["## Current Work Context", f"Path: {breadcrumb_str}"]
    context_lines.append(
        f"Type: {task.get('task_type', 'task')} | Status: {task['status']} | Priority: {task.get('priority', 'medium')}"
    )

    if progress:
        context_lines.append(
            f"Progress: {progress['completed']}/{progress['total']} completed ({progress['percent']}%)"
        )
    if siblings:
        sib_strs = [f"{s['title']} (#{s['id']}/{s['status']})" for s in siblings[:5]]
        context_lines.append(f"Siblings: {', '.join(sib_strs)}")
    if children:
        child_strs = [f"{c['title']} (#{c['id']}/{c['status']})" for c in children[:8]]
        context_lines.append(f"Children: {', '.join(child_strs)}")

    return {
        "task_id": task_id,
        "breadcrumb": breadcrumb,
        "siblings": siblings,
        "children": children,
        "progress": progress,
        "context_text": "\n".join(context_lines),
    }


@app.patch("/api/tasks/{task_id}/reorder", response_model=TaskView)
async def api_reorder_task(task_id: int, body: TaskReorder, request: Request):
    return await services.reorder_task(_db(request), task_id, body)


# --- Approve / Reject / Start ---


@app.post("/api/tasks/{task_id}/approve", response_model=TaskView)
async def api_approve_task(
    task_id: int, request: Request, body: TaskApprove | None = None
):
    return await services.approve_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/reject", response_model=TaskView)
async def api_reject_task(
    task_id: int, request: Request, body: TaskReject | None = None
):
    return await services.reject_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/start", response_model=TaskView)
async def api_start_task(task_id: int, request: Request, body: TaskStart | None = None):
    return await services.start_task(_db(request), task_id, body)


# --- Q&A: Question / Answer ---


@app.post("/api/tasks/{task_id}/question", response_model=TaskView)
async def api_task_question(task_id: int, body: TaskQuestion, request: Request):
    return await services.ask_question(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/answer", response_model=TaskView)
async def api_task_answer(task_id: int, body: TaskAnswer, request: Request):
    return await services.answer_question(_db(request), task_id, body)


# --- Decide (after arbiter) ---


@app.post("/api/tasks/{task_id}/decide", response_model=TaskView)
async def api_decide_task(task_id: int, body: TaskDecide, request: Request):
    return await services.decide_task(_db(request), task_id, body)


# --- Task Updates ---


@app.post("/api/tasks/{task_id}/updates", response_model=TaskUpdateView)
async def api_add_task_update(task_id: int, body: TaskUpdateCreate, request: Request):
    return await services.add_update(_db(request), task_id, body)


@app.get("/api/tasks/{task_id}/updates", response_model=list[TaskUpdateView])
async def api_list_task_updates(task_id: int, request: Request):
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    updates = await repo.get_task_updates(db, task_id)
    return [TaskUpdateView(**dict(u)) for u in updates]


@app.post("/api/tasks/{task_id}/refresh", response_model=TaskView)
async def api_refresh_task(task_id: int, request: Request):
    return await services.refresh_task(_db(request), task_id)


# ---------------------------------------------------------------------------
# Full log endpoints
# ---------------------------------------------------------------------------


@app.get("/api/tasks/{task_id}/log")
async def api_task_log(task_id: int, request: Request, job: str = Query("main")):
    """Return full dispatch log. ?job=main (default) or ?job=review."""
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    job_id = task.get("review_job_id") if job == "review" else task.get("job_id")
    if not job_id:
        raise HTTPException(404, f"no {job} job_id for this task")
    content = plugins.dispatch.job_log_full(job_id)
    if not content:
        raise HTTPException(404, f"log file not found for job {job_id}")
    return PlainTextResponse(content)


# ---------------------------------------------------------------------------
# Deprecated — Proposal endpoints (backward compatibility)
# ---------------------------------------------------------------------------


@app.post("/api/proposals", response_model=TaskView)
async def api_create_proposal_compat(request: Request):
    """Deprecated: creates a draft task instead of a proposal."""
    raw = await request.json()
    body = TaskCreate(
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        source=TaskSource.agent,
        agent=raw.get("agent", ""),
        rationale=raw.get("rationale", ""),
    )
    return await services.create_task(_db(request), body)


@app.get("/api/proposals", response_model=list[TaskView])
async def api_list_proposals_compat(
    request: Request,
    status: str | None = None,
    limit: int = Query(default=50, le=200),
):
    """Deprecated: lists draft/rejected tasks instead of proposals."""
    db = _db(request)
    status_map = {"pending": "draft", "approved": "open", "rejected": "rejected"}
    mapped = status_map.get(status, status) if status else None
    rows = await repo.list_agent_tasks(db, mapped, limit=limit)
    return [services.row_to_task(r) for r in rows]


@app.post("/api/proposals/{proposal_id}/action", response_model=TaskView)
async def api_proposal_action_compat(proposal_id: int, request: Request):
    """Deprecated: approve/reject via the old proposal action format."""
    raw = await request.json()
    action = raw.get("action", "")
    comment = raw.get("comment", "")
    if action == "approved":
        body = TaskApprove(comment=comment, run=True)
        return await services.approve_task(_db(request), proposal_id, body)
    elif action == "rejected":
        body = TaskReject(comment=comment)
        return await services.reject_task(_db(request), proposal_id, body)
    raise HTTPException(400, f"unknown action: {action}")


# ---------------------------------------------------------------------------
# REST API — Dashboard data
# ---------------------------------------------------------------------------


@app.get("/api/dashboard", response_model=DashboardData)
async def api_dashboard(request: Request):
    return await services.get_dashboard_data(_db(request))


@app.get("/api/activity", response_model=list[ActivityItem])
async def api_activity(request: Request, limit: int = Query(default=30, le=100)):
    return await services.list_activity(_db(request), limit=limit)


@app.get("/api/dispatch/jobs", response_model=list[dict[str, Any]])
async def api_dispatch_jobs(limit: int = Query(default=30, le=100)):
    return plugins.dispatch.list_jobs(limit)


@app.get("/api/transcripts", response_model=list[dict[str, Any]])
async def api_transcripts(limit: int = Query(default=10, le=30)):
    return plugins.transcripts.list_recent_transcripts(limit)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    uvicorn.run("hub.app:app", host=config.HUB_HOST, port=config.HUB_PORT, reload=False)


if __name__ == "__main__":
    main()
