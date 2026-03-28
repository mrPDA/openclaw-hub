"""OpenClaw Hub — FastAPI application with REST API and web dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from hub import config
from hub.db import get_db, log_activity
from hub.integrations import dispatch, github, notes, transcripts
from hub.models import (
    ActivityItem,
    DashboardData,
    RuntimeChoice,
    TaskAnswer,
    TaskApprove,
    TaskCreate,
    TaskDecide,
    TaskQuestion,
    TaskReject,
    TaskSource,
    TaskStart,
    TaskStatus,
    TaskUpdateCreate,
    TaskUpdateView,
    TaskView,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("hub")

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))


POLL_INTERVAL = 30  # seconds


async def _dispatch_review(db: aiosqlite.Connection, task: dict[str, Any]) -> None:
    """Dispatch a code-review job for a completed task."""
    task_id = task["id"]
    review_cycle = task.get("review_cycle", 0)
    message = dispatch.build_review_message(
        task_id=task_id,
        title=task["title"],
        description=task.get("description", ""),
        review_cycle=review_cycle,
        max_cycles=config.MAX_REVIEW_CYCLES,
    )
    result = await dispatch.submit_task(message, runtime=config.REVIEW_RUNTIME)
    review_job_id = result.get("job_id")
    if review_job_id:
        await db.execute(
            "UPDATE tasks SET status='review', review_job_id=?, updated_at=datetime('now') WHERE id=?",
            (review_job_id, task_id),
        )
        log.info("Poll: task #%d → review (job=%s, cycle=%d)", task_id, review_job_id, review_cycle + 1)
    else:
        log.warning("Poll: failed to dispatch review for task #%d: %s", task_id, result.get("error"))
        await db.execute(
            "UPDATE tasks SET status='completed', updated_at=datetime('now') WHERE id=?",
            (task_id,),
        )
    await db.commit()


async def _dispatch_fix(db: aiosqlite.Connection, task: dict[str, Any], review_comments: str) -> None:
    """Dispatch a fix job back to the developer agent."""
    task_id = task["id"]
    review_cycle = task.get("review_cycle", 0) + 1
    message = dispatch.build_fix_message(
        task_id=task_id,
        title=task["title"],
        description=task.get("description", ""),
        review_comments=review_comments,
        review_cycle=review_cycle,
        max_cycles=config.MAX_REVIEW_CYCLES,
    )
    runtime = task.get("runtime", "auto")
    result = await dispatch.submit_task(message, runtime=runtime)
    job_id = result.get("job_id")
    if job_id:
        await db.execute(
            "UPDATE tasks SET status='fix_requested', job_id=?, review_cycle=?, updated_at=datetime('now') WHERE id=?",
            (job_id, review_cycle, task_id),
        )
        log.info("Poll: task #%d → fix_requested (job=%s, cycle=%d/%d)",
                 task_id, job_id, review_cycle, config.MAX_REVIEW_CYCLES)
    else:
        log.warning("Poll: failed to dispatch fix for task #%d: %s", task_id, result.get("error"))
        await db.execute(
            "UPDATE tasks SET status='completed', review_cycle=?, updated_at=datetime('now') WHERE id=?",
            (review_cycle, task_id),
        )
    await db.commit()


async def _dispatch_arbiter(db: aiosqlite.Connection, task: dict[str, Any], updates_list: list[dict[str, Any]]) -> None:
    """Dispatch an arbiter (Claude Sonnet) when review cycle limit is reached."""
    task_id = task["id"]
    review_cycle = task.get("review_cycle", 0)

    review_history = [
        u for u in updates_list
        if u.get("kind") in ("review", "done", "status", "alert")
    ]

    await db.execute(
        "INSERT INTO task_updates (task_id, agent, kind, content) VALUES (?, 'hub', 'alert', ?)",
        (task_id, f"Review cycle limit reached ({review_cycle}/{config.MAX_REVIEW_CYCLES}). Dispatching arbiter for independent assessment."),
    )
    await db.commit()
    await log_activity(db, "review_cycle_limit", f"Task #{task_id}: review cycle limit ({review_cycle}/{config.MAX_REVIEW_CYCLES}), dispatching arbiter")

    message = dispatch.build_arbiter_message(
        task_id=task_id,
        title=task["title"],
        description=task.get("description", ""),
        review_history=review_history,
        review_cycle=review_cycle,
        max_cycles=config.MAX_REVIEW_CYCLES,
    )
    result = await dispatch.submit_task(message, runtime=config.ARBITER_RUNTIME)
    arbiter_job_id = result.get("job_id")
    if arbiter_job_id:
        await db.execute(
            "UPDATE tasks SET status='review', review_job_id=?, updated_at=datetime('now') WHERE id=?",
            (arbiter_job_id, task_id),
        )
        log.info("Poll: task #%d → arbiter review (job=%s)", task_id, arbiter_job_id)
    else:
        log.warning("Poll: failed to dispatch arbiter for task #%d: %s", task_id, result.get("error"))
        await db.execute(
            "UPDATE tasks SET status='needs_decision', updated_at=datetime('now') WHERE id=?",
            (task_id,),
        )
    await db.commit()


def _extract_review_verdict(task_id: int, review_job_id: str, db_updates: list[dict[str, Any]]) -> str | None:
    """Return 'approved' or 'changes_requested' from the review update or log tail."""
    for u in reversed(db_updates):
        if u.get("kind") == "review":
            text = u.get("content", "").strip().lower()
            if "approved" in text.split("\n")[-1]:
                return "approved"
            if "changes_requested" in text.split("\n")[-1]:
                return "changes_requested"

    log_lines = dispatch.job_log_tail(review_job_id, max_lines=30)
    combined = "\n".join(log_lines).strip().lower()
    last_line = combined.split("\n")[-1] if combined else ""
    if "approved" in last_line:
        return "approved"
    if "changes_requested" in last_line:
        return "changes_requested"
    return None


async def _poll_running_tasks(app: FastAPI) -> None:
    """Background task: sync running/review/fix_requested tasks with dispatch job status."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            db = app.state.db

            rows = await db.execute_fetchall(
                "SELECT * FROM tasks WHERE status IN ('running', 'fix_requested') AND job_id IS NOT NULL"
            )
            for row in rows:
                task = dict(row)
                job = dispatch.get_job(task["job_id"])
                if not job:
                    continue
                job_status = job.get("status")
                if job_status not in ("completed", "failed"):
                    continue

                if job_status == "failed":
                    await db.execute(
                        "UPDATE tasks SET status='failed', exit_code=?, result_text=?, updated_at=datetime('now') WHERE id=?",
                        (job.get("exit_code"), job.get("result_text"), task["id"]),
                    )
                    await db.commit()
                    log.info("Poll: task #%d → failed (exit=%s)", task["id"], job.get("exit_code"))
                    continue

                if task.get("auto_review") and task.get("review_cycle", 0) < config.MAX_REVIEW_CYCLES:
                    await db.execute(
                        "UPDATE tasks SET exit_code=?, result_text=?, updated_at=datetime('now') WHERE id=?",
                        (job.get("exit_code"), job.get("result_text"), task["id"]),
                    )
                    await db.commit()
                    await _dispatch_review(db, task)
                else:
                    await db.execute(
                        "UPDATE tasks SET status='completed', exit_code=?, result_text=?, updated_at=datetime('now') WHERE id=?",
                        (job.get("exit_code"), job.get("result_text"), task["id"]),
                    )
                    await db.commit()
                    log.info("Poll: task #%d → completed (exit=%s)", task["id"], job.get("exit_code"))

            review_rows = await db.execute_fetchall(
                "SELECT * FROM tasks WHERE status='review' AND review_job_id IS NOT NULL"
            )
            for row in review_rows:
                task = dict(row)
                job = dispatch.get_job(task["review_job_id"])
                if not job:
                    continue
                job_status = job.get("status")
                if job_status not in ("completed", "failed"):
                    continue

                if job_status == "failed":
                    await db.execute(
                        "UPDATE tasks SET status='completed', updated_at=datetime('now') WHERE id=?",
                        (task["id"],),
                    )
                    await db.commit()
                    log.info("Poll: review job failed for task #%d, marking completed", task["id"])
                    continue

                updates_rows = await db.execute_fetchall(
                    "SELECT * FROM task_updates WHERE task_id=? ORDER BY id ASC", (task["id"],)
                )
                updates_list = [dict(r) for r in updates_rows]
                verdict = _extract_review_verdict(task["id"], task["review_job_id"], updates_list)

                has_arbitration = any(u.get("kind") == "arbitration" for u in updates_list)

                if has_arbitration:
                    await db.execute(
                        "UPDATE tasks SET status='needs_decision', updated_at=datetime('now') WHERE id=?",
                        (task["id"],),
                    )
                    await db.commit()
                    log.info("Poll: task #%d arbiter done → needs_decision", task["id"])
                    continue

                if verdict == "approved":
                    await db.execute(
                        "UPDATE tasks SET status='completed', updated_at=datetime('now') WHERE id=?",
                        (task["id"],),
                    )
                    await db.commit()
                    log.info("Poll: task #%d review → approved", task["id"])
                elif verdict == "changes_requested":
                    if task.get("review_cycle", 0) + 1 >= config.MAX_REVIEW_CYCLES:
                        await db.execute(
                            "UPDATE tasks SET review_cycle=?, updated_at=datetime('now') WHERE id=?",
                            (task.get("review_cycle", 0) + 1, task["id"]),
                        )
                        await db.commit()
                        await _dispatch_arbiter(db, task, updates_list)
                    else:
                        review_text = ""
                        for u in reversed(updates_list):
                            if u.get("kind") == "review":
                                review_text = u.get("content", "")
                                break
                        await _dispatch_fix(db, task, review_text)
                else:
                    log.info("Poll: task #%d review job done but no clear verdict, marking completed", task["id"])
                    await db.execute(
                        "UPDATE tasks SET status='completed', updated_at=datetime('now') WHERE id=?",
                        (task["id"],),
                    )
                    await db.commit()

        except Exception:
            log.exception("Poll error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await get_db()
    log.info("Hub database ready at %s", config.HUB_DB_PATH)
    poll_task = asyncio.create_task(_poll_running_tasks(app))
    log.info("Background poller started (every %ds)", POLL_INTERVAL)
    yield
    poll_task.cancel()
    await app.state.db.close()


app = FastAPI(title="OpenClaw Hub", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def _db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_task(row: aiosqlite.Row, updates: list[aiosqlite.Row] | None = None) -> TaskView:
    d = dict(row)
    log_tail = None
    if d.get("job_id"):
        log_tail = dispatch.job_log_tail(d["job_id"], max_lines=20)
    upd_list = [TaskUpdateView(**dict(u)) for u in updates] if updates else None
    source = d.get("source", "human")
    assigned_agent = d.get("assigned_agent", "")
    rationale = d.get("rationale", "")
    return TaskView(
        id=d["id"],
        title=d["title"],
        description=d.get("description", ""),
        status=d["status"],
        runtime=d.get("runtime", "auto"),
        source=source,
        assigned_agent=assigned_agent,
        rationale=rationale,
        job_id=d.get("job_id"),
        exit_code=d.get("exit_code"),
        result_text=d.get("result_text"),
        log_tail=log_tail,
        updates=upd_list,
        review_cycle=d.get("review_cycle", 0),
        auto_review=bool(d.get("auto_review", 1)),
        review_job_id=d.get("review_job_id"),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
    )


async def _dispatch_task(db: aiosqlite.Connection, task_id: int, task: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a task via oc-dev-dispatch and update DB status."""
    updates_rows = await db.execute_fetchall(
        "SELECT * FROM task_updates WHERE task_id=? ORDER BY id ASC", (task_id,)
    )
    updates = [dict(r) for r in updates_rows] if updates_rows else None

    message = dispatch.build_enriched_message(task["title"], task.get("description", ""), updates)
    runtime = task.get("runtime", "auto")
    result = await dispatch.submit_task(message, runtime=runtime)
    job_id = result.get("job_id")

    if job_id:
        await db.execute(
            "UPDATE tasks SET status='running', job_id=?, updated_at=datetime('now') WHERE id=?",
            (job_id, task_id),
        )
    else:
        error = result.get("error", "dispatch returned no job_id")
        await db.execute(
            "UPDATE tasks SET status='failed', result_text=?, updated_at=datetime('now') WHERE id=?",
            (error, task_id),
        )
    await db.commit()
    return result


# ---------------------------------------------------------------------------
# REST API — Tasks
# ---------------------------------------------------------------------------

@app.post("/api/tasks", response_model=TaskView)
async def api_create_task(body: TaskCreate, request: Request):
    db = _db(request)

    if body.source == TaskSource.agent:
        initial_status = "draft"
    elif body.run_immediately:
        initial_status = "running"
    else:
        initial_status = "open"

    cur = await db.execute(
        "INSERT INTO tasks (title, description, runtime, source, assigned_agent, rationale, status, auto_review) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (body.title, body.description, body.runtime.value, body.source.value,
         body.agent, body.rationale, initial_status, int(body.auto_review)),
    )
    task_id = cur.lastrowid
    await db.commit()

    result: dict[str, Any] = {}
    if body.run_immediately and body.source != TaskSource.agent:
        row = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
        result = await _dispatch_task(db, task_id, dict(row[0]))

    await log_activity(db, "task_created", f"Task #{task_id}: {body.title}", json.dumps(result, ensure_ascii=False) if result else None)

    row = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    return _row_to_task(row[0])


@app.get("/api/tasks", response_model=list[TaskView])
async def api_list_tasks(
    request: Request,
    status: str | None = None,
    limit: int = Query(default=50, le=200),
):
    db = _db(request)
    if status:
        rows = await db.execute_fetchall(
            "SELECT * FROM tasks WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit),
        )
    else:
        rows = await db.execute_fetchall("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
    return [_row_to_task(r) for r in rows]


@app.get("/api/tasks/{task_id}", response_model=TaskView)
async def api_get_task(task_id: int, request: Request):
    db = _db(request)
    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "task not found")
    updates = await db.execute_fetchall(
        "SELECT * FROM task_updates WHERE task_id=? ORDER BY id ASC", (task_id,),
    )
    return _row_to_task(rows[0], updates=updates)


# --- Approve / Reject / Start ---

@app.post("/api/tasks/{task_id}/approve", response_model=TaskView)
async def api_approve_task(task_id: int, request: Request, body: TaskApprove | None = None):
    db = _db(request)
    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "task not found")
    task = dict(rows[0])
    if task["status"] != "draft":
        raise HTTPException(400, f"can only approve draft tasks, current status: {task['status']}")

    body = body or TaskApprove()
    new_status = "open"

    if body.comment:
        await db.execute(
            "INSERT INTO task_updates (task_id, agent, kind, content) VALUES (?, '', 'status', ?)",
            (task_id, f"Approved: {body.comment}"),
        )

    if body.runtime:
        await db.execute("UPDATE tasks SET runtime=? WHERE id=?", (body.runtime.value, task_id))
        task["runtime"] = body.runtime.value

    await db.execute(
        "UPDATE tasks SET status=?, updated_at=datetime('now') WHERE id=?",
        (new_status, task_id),
    )
    await db.commit()

    if body.run:
        task["status"] = new_status
        await _dispatch_task(db, task_id, task)

    await log_activity(db, "task_approved", f"Task #{task_id} approved" + (f" (run={body.run})" if body.run else ""))

    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    updates = await db.execute_fetchall("SELECT * FROM task_updates WHERE task_id=? ORDER BY id ASC", (task_id,))
    return _row_to_task(rows[0], updates=updates)


@app.post("/api/tasks/{task_id}/reject", response_model=TaskView)
async def api_reject_task(task_id: int, request: Request, body: TaskReject | None = None):
    db = _db(request)
    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "task not found")
    task = dict(rows[0])
    if task["status"] != "draft":
        raise HTTPException(400, f"can only reject draft tasks, current status: {task['status']}")

    body = body or TaskReject()
    if body.comment:
        await db.execute(
            "INSERT INTO task_updates (task_id, agent, kind, content) VALUES (?, '', 'status', ?)",
            (task_id, f"Rejected: {body.comment}"),
        )

    await db.execute(
        "UPDATE tasks SET status='rejected', updated_at=datetime('now') WHERE id=?",
        (task_id,),
    )
    await db.commit()
    await log_activity(db, "task_rejected", f"Task #{task_id} rejected")

    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    return _row_to_task(rows[0])


@app.post("/api/tasks/{task_id}/start", response_model=TaskView)
async def api_start_task(task_id: int, request: Request, body: TaskStart | None = None):
    db = _db(request)
    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "task not found")
    task = dict(rows[0])
    if task["status"] != "open":
        raise HTTPException(400, f"can only start open tasks, current status: {task['status']}")

    body = body or TaskStart()
    if body.runtime:
        await db.execute("UPDATE tasks SET runtime=? WHERE id=?", (body.runtime.value, task_id))
        task["runtime"] = body.runtime.value

    await _dispatch_task(db, task_id, task)
    await log_activity(db, "task_started", f"Task #{task_id} dispatched")

    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    updates = await db.execute_fetchall("SELECT * FROM task_updates WHERE task_id=? ORDER BY id ASC", (task_id,))
    return _row_to_task(rows[0], updates=updates)


# --- Q&A: Question / Answer ---

@app.post("/api/tasks/{task_id}/question", response_model=TaskView)
async def api_task_question(task_id: int, body: TaskQuestion, request: Request):
    db = _db(request)
    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "task not found")
    task = dict(rows[0])
    if task["status"] != "running":
        raise HTTPException(400, f"can only ask questions on running tasks, current status: {task['status']}")

    await db.execute(
        "INSERT INTO task_updates (task_id, agent, kind, content) VALUES (?, ?, 'question', ?)",
        (task_id, body.agent, body.question),
    )
    await db.execute(
        "UPDATE tasks SET status='needs_info', updated_at=datetime('now') WHERE id=?",
        (task_id,),
    )
    await db.commit()
    await log_activity(db, "task_question", f"Task #{task_id}: agent asked a question")

    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    updates = await db.execute_fetchall("SELECT * FROM task_updates WHERE task_id=? ORDER BY id ASC", (task_id,))
    return _row_to_task(rows[0], updates=updates)


@app.post("/api/tasks/{task_id}/answer", response_model=TaskView)
async def api_task_answer(task_id: int, body: TaskAnswer, request: Request):
    db = _db(request)
    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "task not found")
    task = dict(rows[0])
    if task["status"] != "needs_info":
        raise HTTPException(400, f"can only answer needs_info tasks, current status: {task['status']}")

    await db.execute(
        "INSERT INTO task_updates (task_id, agent, kind, content) VALUES (?, '', 'answer', ?)",
        (task_id, body.answer),
    )
    await db.commit()

    if body.resume:
        task_row = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
        await _dispatch_task(db, task_id, dict(task_row[0]))
        await log_activity(db, "task_answered", f"Task #{task_id}: answered and re-dispatched")
    else:
        await db.execute(
            "UPDATE tasks SET status='open', updated_at=datetime('now') WHERE id=?",
            (task_id,),
        )
        await db.commit()
        await log_activity(db, "task_answered", f"Task #{task_id}: answered, moved to open")

    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    updates = await db.execute_fetchall("SELECT * FROM task_updates WHERE task_id=? ORDER BY id ASC", (task_id,))
    return _row_to_task(rows[0], updates=updates)


# --- Decide (after arbiter) ---

@app.post("/api/tasks/{task_id}/decide", response_model=TaskView)
async def api_decide_task(task_id: int, body: TaskDecide, request: Request):
    db = _db(request)
    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "task not found")
    task = dict(rows[0])
    if task["status"] != "needs_decision":
        raise HTTPException(400, f"can only decide on needs_decision tasks, current status: {task['status']}")

    if body.action == "accept":
        await db.execute(
            "INSERT INTO task_updates (task_id, agent, kind, content) VALUES (?, 'human', 'status', ?)",
            (task_id, "Human accepted task after arbiter review."),
        )
        await db.execute(
            "UPDATE tasks SET status='completed', updated_at=datetime('now') WHERE id=?",
            (task_id,),
        )
        await db.commit()
        await log_activity(db, "task_decided", f"Task #{task_id}: accepted after arbitration")
    else:
        instructions = body.instructions or "Fix remaining issues."
        await db.execute(
            "INSERT INTO task_updates (task_id, agent, kind, content) VALUES (?, 'human', 'status', ?)",
            (task_id, f"Human requested rework after arbiter review: {instructions}"),
        )
        await db.execute(
            "UPDATE tasks SET review_cycle=0, updated_at=datetime('now') WHERE id=?",
            (task_id,),
        )
        await db.commit()

        message = dispatch.build_fix_message(
            task_id=task_id,
            title=task["title"],
            description=task.get("description", ""),
            review_comments=instructions,
            review_cycle=0,
            max_cycles=config.MAX_REVIEW_CYCLES,
        )
        runtime = task.get("runtime", "auto")
        result = await dispatch.submit_task(message, runtime=runtime)
        job_id = result.get("job_id")
        if job_id:
            await db.execute(
                "UPDATE tasks SET status='fix_requested', job_id=?, updated_at=datetime('now') WHERE id=?",
                (job_id, task_id),
            )
        else:
            await db.execute(
                "UPDATE tasks SET status='open', updated_at=datetime('now') WHERE id=?",
                (task_id,),
            )
        await db.commit()
        await log_activity(db, "task_decided", f"Task #{task_id}: rework requested after arbitration")

    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    updates = await db.execute_fetchall("SELECT * FROM task_updates WHERE task_id=? ORDER BY id ASC", (task_id,))
    return _row_to_task(rows[0], updates=updates)


# --- Task Updates ---

@app.post("/api/tasks/{task_id}/updates", response_model=TaskUpdateView)
async def api_add_task_update(task_id: int, body: TaskUpdateCreate, request: Request):
    db = _db(request)
    rows = await db.execute_fetchall("SELECT id FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "task not found")
    cur = await db.execute(
        "INSERT INTO task_updates (task_id, agent, kind, content) VALUES (?, ?, ?, ?)",
        (task_id, body.agent, body.kind, body.content),
    )
    await db.execute(
        "UPDATE tasks SET updated_at=datetime('now') WHERE id=?", (task_id,),
    )
    await db.commit()
    await log_activity(db, "task_update", f"Task #{task_id} update from {body.agent}: {body.content[:80]}")
    update_rows = await db.execute_fetchall(
        "SELECT * FROM task_updates WHERE id=?", (cur.lastrowid,),
    )
    return TaskUpdateView(**dict(update_rows[0]))


@app.get("/api/tasks/{task_id}/updates", response_model=list[TaskUpdateView])
async def api_list_task_updates(task_id: int, request: Request):
    db = _db(request)
    rows = await db.execute_fetchall("SELECT id FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "task not found")
    updates = await db.execute_fetchall(
        "SELECT * FROM task_updates WHERE task_id=? ORDER BY id ASC", (task_id,),
    )
    return [TaskUpdateView(**dict(u)) for u in updates]


@app.post("/api/tasks/{task_id}/refresh")
async def api_refresh_task(task_id: int, request: Request):
    db = _db(request)
    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "task not found")
    task = dict(rows[0])
    job_id = task.get("job_id")
    if not job_id:
        return _row_to_task(rows[0])

    job = dispatch.get_job(job_id)
    if job:
        new_status = task["status"]
        if job.get("status") == "completed":
            new_status = "completed"
        elif job.get("status") == "failed":
            new_status = "failed"
        elif job.get("status") == "running":
            new_status = "running"

        await db.execute(
            "UPDATE tasks SET status=?, exit_code=?, result_text=?, updated_at=datetime('now') WHERE id=?",
            (new_status, job.get("exit_code"), job.get("result_text"), task_id),
        )
        await db.commit()

    rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id=?", (task_id,))
    return _row_to_task(rows[0])


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
    return await api_create_task(body, request)


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
    if mapped:
        rows = await db.execute_fetchall(
            "SELECT * FROM tasks WHERE source='agent' AND status=? ORDER BY id DESC LIMIT ?",
            (mapped, limit),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM tasks WHERE source='agent' ORDER BY id DESC LIMIT ?", (limit,),
        )
    return [_row_to_task(r) for r in rows]


@app.post("/api/proposals/{proposal_id}/action", response_model=TaskView)
async def api_proposal_action_compat(proposal_id: int, request: Request):
    """Deprecated: approve/reject via the old proposal action format."""
    raw = await request.json()
    action = raw.get("action", "")
    comment = raw.get("comment", "")
    if action == "approved":
        body = TaskApprove(comment=comment, run=True)
        return await api_approve_task(proposal_id, request, body)
    elif action == "rejected":
        body = TaskReject(comment=comment)
        return await api_reject_task(proposal_id, request, body)
    raise HTTPException(400, f"unknown action: {action}")


# ---------------------------------------------------------------------------
# REST API — Dashboard data
# ---------------------------------------------------------------------------

@app.get("/api/dashboard", response_model=DashboardData)
async def api_dashboard(request: Request):
    db = _db(request)

    commits_t = asyncio.create_task(github.recent_commits(8))
    prs_t = asyncio.create_task(github.open_prs())
    decisions_t = asyncio.create_task(notes.recent_decisions(limit=8))

    active_rows = await db.execute_fetchall(
        "SELECT * FROM tasks WHERE status IN ('open','running','fix_requested') ORDER BY id DESC LIMIT 20",
    )
    draft_rows = await db.execute_fetchall(
        "SELECT * FROM tasks WHERE status='draft' ORDER BY id DESC LIMIT 20",
    )
    needs_info_rows = await db.execute_fetchall(
        "SELECT * FROM tasks WHERE status='needs_info' ORDER BY id DESC LIMIT 20",
    )
    review_rows = await db.execute_fetchall(
        "SELECT * FROM tasks WHERE status='review' ORDER BY id DESC LIMIT 20",
    )
    needs_decision_rows = await db.execute_fetchall(
        "SELECT * FROM tasks WHERE status='needs_decision' ORDER BY id DESC LIMIT 20",
    )

    commits = await commits_t
    prs = await prs_t
    decisions = await decisions_t

    return DashboardData(
        recent_commits=commits,
        open_prs=prs,
        active_tasks=[_row_to_task(r) for r in active_rows],
        draft_tasks=[_row_to_task(r) for r in draft_rows],
        needs_info_tasks=[_row_to_task(r) for r in needs_info_rows],
        review_tasks=[_row_to_task(r) for r in review_rows],
        needs_decision_tasks=[_row_to_task(r) for r in needs_decision_rows],
        recent_decisions=decisions,
    )


@app.get("/api/activity", response_model=list[ActivityItem])
async def api_activity(request: Request, limit: int = Query(default=30, le=100)):
    db = _db(request)
    rows = await db.execute_fetchall(
        "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,),
    )
    result: list[ActivityItem] = []
    for r in rows:
        d = dict(r)
        detail = None
        if d.get("detail"):
            try:
                detail = json.loads(d["detail"])
            except json.JSONDecodeError:
                detail = {"raw": d["detail"]}
        result.append(ActivityItem(kind=d["kind"], summary=d["summary"], detail=detail, timestamp=d["timestamp"]))
    return result


@app.get("/api/dispatch/jobs")
async def api_dispatch_jobs(limit: int = Query(default=30, le=100)):
    return dispatch.list_jobs(limit)


@app.get("/api/transcripts")
async def api_transcripts(limit: int = Query(default=10, le=30)):
    return transcripts.list_recent_transcripts(limit)


# ---------------------------------------------------------------------------
# Web UI routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def web_dashboard(request: Request):
    data = await api_dashboard(request)
    return TEMPLATES.TemplateResponse(request, "dashboard.html", {"data": data})


@app.get("/tasks", response_class=HTMLResponse)
async def web_tasks(request: Request):
    tasks = await api_list_tasks(request, limit=100)
    return TEMPLATES.TemplateResponse(request, "tasks.html", {"tasks": tasks})


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def web_task_detail(task_id: int, request: Request):
    task = await api_get_task(task_id, request)
    return TEMPLATES.TemplateResponse(request, "task_detail.html", {"task": task})


@app.post("/tasks/create", response_class=HTMLResponse)
async def web_create_task(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    runtime: str = Form("auto"),
    run_immediately: bool = Form(False),
):
    body = TaskCreate(title=title, description=description, runtime=runtime, run_immediately=run_immediately)
    await api_create_task(body, request)
    return RedirectResponse("/tasks", status_code=303)


@app.post("/tasks/{task_id}/web-approve")
async def web_approve_task(task_id: int, request: Request, comment: str = Form(""), run: bool = Form(False), runtime: str = Form("auto")):
    body = TaskApprove(comment=comment, run=run, runtime=RuntimeChoice(runtime) if runtime else None)
    await api_approve_task(task_id, request, body)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/web-reject")
async def web_reject_task(task_id: int, request: Request, comment: str = Form("")):
    body = TaskReject(comment=comment)
    await api_reject_task(task_id, request, body)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/web-start")
async def web_start_task(task_id: int, request: Request, runtime: str = Form("auto")):
    body = TaskStart(runtime=RuntimeChoice(runtime) if runtime else None)
    await api_start_task(task_id, request, body)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/web-answer")
async def web_answer_task(task_id: int, request: Request, answer: str = Form(...), resume: bool = Form(True)):
    body = TaskAnswer(answer=answer, resume=resume)
    await api_task_answer(task_id, body, request)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/web-decide")
async def web_decide_task(task_id: int, request: Request, action: str = Form(...), instructions: str = Form("")):
    body = TaskDecide(action=action, instructions=instructions)
    await api_decide_task(task_id, body, request)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


# Deprecated web proposal routes
@app.post("/proposals/{proposal_id}/approve")
async def web_approve_proposal_compat(proposal_id: int, request: Request, comment: str = Form("")):
    body = TaskApprove(comment=comment, run=True)
    await api_approve_task(proposal_id, request, body)
    return RedirectResponse("/", status_code=303)


@app.post("/proposals/{proposal_id}/reject")
async def web_reject_proposal_compat(proposal_id: int, request: Request, comment: str = Form("")):
    body = TaskReject(comment=comment)
    await api_reject_task(proposal_id, request, body)
    return RedirectResponse("/", status_code=303)


@app.get("/proposals", response_class=HTMLResponse)
async def web_proposals_compat(request: Request):
    """Deprecated: redirects to tasks filtered by agent source."""
    return RedirectResponse("/tasks?source=agent", status_code=302)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    uvicorn.run("hub.app:app", host=config.HUB_HOST, port=config.HUB_PORT, reload=False)


if __name__ == "__main__":
    main()
