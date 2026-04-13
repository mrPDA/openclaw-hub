"""OpenClaw Hub MCP server — exposes hub tools for Cursor and remote agents."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "openclaw-hub",
    instructions="MCP server for OpenClaw Hub — project state, tasks, proposals, decisions",
)


def _hub_url() -> str:
    import os

    return os.environ.get("OPENCLAW_HUB_URL", "http://127.0.0.1:8080")


async def _api_get(path: str) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{_hub_url()}{path}")
        resp.raise_for_status()
        return resp.json()


async def _api_post(path: str, body: dict[str, Any] | None = None) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{_hub_url()}{path}", json=body or {})
        resp.raise_for_status()
        return resp.json()


async def _api_patch(path: str, body: dict[str, Any] | None = None) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(f"{_hub_url()}{path}", json=body or {})
        resp.raise_for_status()
        return resp.json()


def _format_task(t: dict[str, Any]) -> str:
    src = (
        f" [agent:{t.get('assigned_agent', '')}]" if t.get("source") == "agent" else ""
    )
    tt = t.get("task_type", "task")
    tt_tag = f"[{tt}] " if tt != "task" else ""
    parent = f" (parent #{t['parent_id']})" if t.get("parent_id") else ""
    return f"#{t['id']} {tt_tag}[{t['status']}] ({t.get('runtime', 'auto')}){src}{parent} {t['title']}"


# ---------------------------------------------------------------------------
# Dashboard / Overview
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_project_status() -> str:
    """Get project overview: active tasks, drafts needing approval, tasks with questions, open PRs, recent commits, decisions."""
    data = await _api_get("/api/dashboard")
    parts: list[str] = []

    drafts = data.get("draft_tasks", [])
    if drafts:
        parts.append("## Drafts (need approval)")
        for t in drafts:
            parts.append(f"- {_format_task(t)}")

    needs_info = data.get("needs_info_tasks", [])
    if needs_info:
        parts.append("\n## Needs Info (agent asked a question)")
        for t in needs_info:
            parts.append(f"- {_format_task(t)}")

    review = data.get("review_tasks", [])
    if review:
        parts.append("\n## Under Review")
        for t in review:
            cycle = t.get("review_cycle", 0)
            parts.append(f"- {_format_task(t)} (review cycle {cycle + 1})")

    needs_decision = data.get("needs_decision_tasks", [])
    if needs_decision:
        parts.append("\n## Needs Decision (arbiter report ready)")
        for t in needs_decision:
            parts.append(
                f"- {_format_task(t)} — ARBITER REPORT READY, human must decide"
            )

    tasks = data.get("active_tasks", [])
    if tasks:
        parts.append("\n## Active Tasks (open / running)")
        for t in tasks:
            parts.append(f"- {_format_task(t)}")

    prs = data.get("open_prs", [])
    if prs:
        parts.append("\n## Open PRs")
        for pr in prs:
            parts.append(
                f"- #{pr['number']} {pr['title']} ({pr.get('headRefName', '')})"
            )

    commits = data.get("recent_commits", [])
    if commits:
        parts.append("\n## Recent Commits")
        for c in commits[:5]:
            msg = c.get("message", "").split("\n")[0][:80]
            parts.append(f"- {c.get('sha', '')} {msg}")

    decisions = data.get("recent_decisions", [])
    if decisions:
        parts.append("\n## Recent Decisions")
        for d in decisions[:5]:
            title = d.get("title", "Decision")
            parts.append(f"- {title}")

    return "\n".join(parts) if parts else "No activity found."


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_create_task(
    title: str,
    description: str = "",
    task_type: str = "task",
    parent_id: int | None = None,
    priority: str = "medium",
    runtime: str = "auto",
    run_immediately: bool = False,
) -> str:
    """Create a new task, epic, feature, or subtask.

    Args:
        title: Short title (required)
        description: Detailed description of what needs to be done
        task_type: 'epic', 'feature', 'task', or 'subtask'
        parent_id: Parent task ID (required for feature/subtask, optional for task)
        priority: 'critical', 'high', 'medium', or 'low'
        runtime: 'auto', 'openrouter', or 'vast'
        run_immediately: If True, dispatch immediately (not applicable for epic/feature)
    """
    body: dict[str, Any] = {
        "title": title,
        "description": description,
        "task_type": task_type,
        "priority": priority,
        "runtime": runtime,
        "source": "human",
        "run_immediately": run_immediately,
    }
    if parent_id is not None:
        body["parent_id"] = parent_id
    result = await _api_post("/api/tasks", body)
    status = result.get("status", "?")
    return (
        f"{task_type.capitalize()} #{result['id']} created (status: {status}).\n"
        + json.dumps(result, ensure_ascii=False, indent=2)
    )


@mcp.tool()
async def hub_list_tasks(
    status: str = "", task_type: str = "", parent_id: int | None = None, limit: int = 20
) -> str:
    """List tasks with optional filters.

    Args:
        status: Filter by status: draft, open, running, needs_info, review, fix_requested, needs_decision, completed, failed, rejected. Empty for all.
        task_type: Filter by type: epic, feature, task, subtask. Empty for all.
        parent_id: Filter by parent task ID. None for all.
        limit: Max number of tasks to return
    """
    params = f"?limit={limit}"
    if status:
        params += f"&status={status}"
    if task_type:
        params += f"&type={task_type}"
    if parent_id is not None:
        params += f"&parent_id={parent_id}"
    tasks = await _api_get(f"/api/tasks{params}")
    if not tasks:
        return "No tasks found."
    lines = [_format_task(t) for t in tasks]
    return "\n".join(lines)


@mcp.tool()
async def hub_task_status(task_id: int) -> str:
    """Get detailed status of a specific task including updates and log tail.

    Args:
        task_id: The task ID number
    """
    await _api_post(f"/api/tasks/{task_id}/refresh")
    task = await _api_get(f"/api/tasks/{task_id}")
    parts = [
        f"Task #{task['id']}: {task['title']}",
        f"Status: {task['status']}",
        f"Source: {task.get('source', 'human')}",
        f"Runtime: {task.get('runtime', 'auto')}",
        f"Agent: {task.get('assigned_agent', '-')}",
        f"Job ID: {task.get('job_id', '-')}",
        f"Exit code: {task.get('exit_code', '-')}",
        f"Review: {'enabled' if task.get('auto_review', True) else 'disabled'}, cycle {task.get('review_cycle', 0)}",
        f"Created: {task['created_at']}",
    ]
    if task.get("updates"):
        parts.append("\nUpdates:")
        for u in task["updates"]:
            parts.append(
                f"  [{u['created_at']}] ({u['kind']}) {u.get('agent', '')}: {u['content']}"
            )
    if task.get("result_text"):
        parts.append(f"\nResult:\n{task['result_text']}")
    if task.get("log_tail"):
        parts.append("\nLog tail:\n" + "\n".join(task["log_tail"][-20:]))
    return "\n".join(parts)


@mcp.tool()
async def hub_task_update(
    task_id: int, content: str, agent: str = "", kind: str = "status"
) -> str:
    """Add a status update or report to a task.

    Args:
        task_id: The task ID to update
        content: Update text — status report, blocker description, or completion report
        agent: Name of the agent posting the update
        kind: Type of update: 'status', 'report', 'blocker', 'done', 'review', or 'arbitration'
    """
    result = await _api_post(
        f"/api/tasks/{task_id}/updates",
        {
            "agent": agent,
            "kind": kind,
            "content": content,
        },
    )
    return f"Update #{result['id']} added to task #{task_id}."


@mcp.tool()
async def hub_report_done(task_id: int, summary: str, agent: str = "") -> str:
    """Submit a completion report for a task. Required to move task from pending_report to completed.

    After an agent finishes work, it must submit a done report describing what was changed
    and how it was validated. This is the only way to complete a pending_report task.

    Args:
        task_id: The task ID to report on
        summary: What was changed and how it was validated (e.g. 'Changed: X, Y. Validation: tests pass, ruff clean.')
        agent: Name of the agent submitting the report
    """
    result = await _api_post(
        f"/api/tasks/{task_id}/updates",
        {
            "agent": agent,
            "kind": "done",
            "content": summary,
        },
    )
    return f"Done report #{result['id']} submitted for task #{task_id}. Task should now be completed."


# ---------------------------------------------------------------------------
# Hierarchy: tree, context
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_task_tree(task_id: int) -> str:
    """Get the hierarchy tree for a task/epic/feature with all descendants and progress.

    Args:
        task_id: The root task ID to build tree from
    """
    tree = await _api_get(f"/api/tasks/{task_id}/tree")

    def _fmt(node: dict[str, Any], indent: int = 0) -> list[str]:
        prefix = "  " * indent
        tt = node.get("task_type", "task")
        progress = node.get("progress")
        prog_str = ""
        if progress and progress.get("total", 0) > 0:
            prog_str = f" ({progress['completed']}/{progress['total']} = {progress['percent']}%)"
        lines = [
            f"{prefix}[{tt}] #{node['id']} {node['title']} — {node['status']}{prog_str}"
        ]
        for child in node.get("children", []):
            lines.extend(_fmt(child, indent + 1))
        return lines

    return "\n".join(_fmt(tree))


@mcp.tool()
async def hub_my_context(task_id: int) -> str:
    """Get full work context for an agent: hierarchy breadcrumb, siblings, progress, children.

    Use this before starting work on a task to understand its place in the project.

    Args:
        task_id: The task ID to get context for
    """
    ctx = await _api_get(f"/api/tasks/{task_id}/context")
    return ctx.get("context_text", f"Context for task #{task_id} not available.")


# ---------------------------------------------------------------------------
# Lifecycle: approve, reject, start
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_approve_task(
    task_id: int, comment: str = "", run: bool = False, runtime: str = ""
) -> str:
    """Approve a draft task (proposed by agent). Optionally dispatch immediately.

    Args:
        task_id: The draft task ID to approve
        comment: Optional reviewer comment
        run: If True, also dispatch the task immediately after approval
        runtime: Override runtime: 'auto', 'openrouter', or 'vast'. Empty to keep existing.
    """
    body: dict[str, Any] = {"comment": comment, "run": run}
    if runtime:
        body["runtime"] = runtime
    result = await _api_post(f"/api/tasks/{task_id}/approve", body)
    status = result.get("status", "?")
    return f"Task #{task_id} approved (status: {status})."


@mcp.tool()
async def hub_reject_task(task_id: int, comment: str = "") -> str:
    """Reject a draft task (proposed by agent).

    Args:
        task_id: The draft task ID to reject
        comment: Reason for rejection
    """
    await _api_post(f"/api/tasks/{task_id}/reject", {"comment": comment})
    return f"Task #{task_id} rejected."


@mcp.tool()
async def hub_start_task(task_id: int, plan: str = "", runtime: str = "") -> str:
    """Dispatch an open task to an agent. Requires a plan.

    A plan is required before starting. Either pass it here or create
    an update with kind='status' and content starting with 'Plan:' beforehand.

    Args:
        task_id: The open task ID to start
        plan: Work plan (what will be done and how). Required if no plan update exists.
        runtime: Override runtime: 'auto', 'openrouter', or 'vast'. Empty to keep existing.
    """
    body: dict[str, Any] = {}
    if plan:
        body["plan"] = plan
    if runtime:
        body["runtime"] = runtime
    result = await _api_post(f"/api/tasks/{task_id}/start", body)
    status = result.get("status", "?")
    job_id = result.get("job_id", "-")
    return f"Task #{task_id} dispatched (status: {status}, job: {job_id})."


# ---------------------------------------------------------------------------
# Q&A: question / answer
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_ask_question(task_id: int, question: str, agent: str = "") -> str:
    """Agent asks a clarifying question on a running task. Task pauses until human answers.

    Args:
        task_id: The running task ID
        question: The question text
        agent: Name of the agent asking
    """
    await _api_post(
        f"/api/tasks/{task_id}/question",
        {
            "agent": agent,
            "question": question,
        },
    )
    return f"Question posted on task #{task_id}. Task is now paused (needs_info). Waiting for human answer."


@mcp.tool()
async def hub_answer_question(task_id: int, answer: str, resume: bool = True) -> str:
    """Human answers agent's question. By default re-dispatches the task.

    Args:
        task_id: The needs_info task ID
        answer: The answer text
        resume: If True, re-dispatch the task with context. If False, just save the answer.
    """
    result = await _api_post(
        f"/api/tasks/{task_id}/answer",
        {
            "answer": answer,
            "resume": resume,
        },
    )
    status = result.get("status", "?")
    return f"Answer posted on task #{task_id} (status: {status})."


# ---------------------------------------------------------------------------
# Decide (after arbiter)
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_decide_task(task_id: int, action: str, instructions: str = "") -> str:
    """Human decision after arbiter review. Accept task as completed or send back for rework.

    Args:
        task_id: The needs_decision task ID
        action: 'accept' to complete, 'rework' to send back for fixes
        instructions: When action='rework', what needs to be fixed
    """
    result = await _api_post(
        f"/api/tasks/{task_id}/decide",
        {
            "action": action,
            "instructions": instructions,
        },
    )
    status = result.get("status", "?")
    return f"Task #{task_id}: decision '{action}' applied (status: {status})."


# ---------------------------------------------------------------------------
# Proposals (backward compat)
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_propose_task(
    title: str,
    description: str,
    agent: str = "",
    rationale: str = "",
    parent_id: int | None = None,
) -> str:
    """Propose a new task for human approval (used by agents). Creates a draft task.

    Args:
        title: Short title of the proposed task
        description: What needs to be done and why
        agent: Name of the proposing agent
        rationale: Why this task is needed
        parent_id: Optional parent task ID (to propose subtask or task within a feature)
    """
    body: dict[str, Any] = {
        "title": title,
        "description": description,
        "source": "agent",
        "agent": agent,
        "rationale": rationale,
    }
    if parent_id is not None:
        body["parent_id"] = parent_id
    result = await _api_post("/api/tasks", body)
    return f"Draft task #{result['id']} created. Awaiting human approval."


@mcp.tool()
async def hub_list_proposals(status: str = "draft") -> str:
    """List agent proposals (draft tasks).

    Args:
        status: Filter: draft, open, rejected. Default: draft.
    """
    tasks = await _api_get(f"/api/tasks?status={status}&limit=50")
    agent_tasks = [t for t in tasks if t.get("source") == "agent"]
    if not agent_tasks:
        return f"No {status} proposals."
    lines = [_format_task(t) for t in agent_tasks]
    return "\n".join(lines)


# Deprecated aliases
@mcp.tool()
async def hub_approve_proposal(proposal_id: int, comment: str = "") -> str:
    """Deprecated: use hub_approve_task instead. Approves and dispatches."""
    return await hub_approve_task(proposal_id, comment=comment, run=True)


@mcp.tool()
async def hub_reject_proposal(proposal_id: int, comment: str = "") -> str:
    """Deprecated: use hub_reject_task instead."""
    return await hub_reject_task(proposal_id, comment=comment)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


@mcp.tool()
async def hub_list_decisions(limit: int = 10) -> str:
    """List recent architectural/development decisions from notesforllm.

    Args:
        limit: Max decisions to return
    """
    data = await _api_get("/api/dashboard")
    decisions = data.get("recent_decisions", [])
    if not decisions:
        return "No decisions recorded."
    lines = []
    for d in decisions[:limit]:
        title = d.get("title", "Decision")
        content = d.get("content", d.get("decision", ""))
        lines.append(f"- {title}")
        if content:
            lines.append(f"  {content[:200]}")
    return "\n".join(lines)


@mcp.tool()
async def hub_dispatch_jobs(limit: int = 15) -> str:
    """List recent oc-dev-dispatch jobs (raw dispatch state).

    Args:
        limit: Max jobs to return
    """
    jobs = await _api_get(f"/api/dispatch/jobs?limit={limit}")
    if not jobs:
        return "No dispatch jobs found."
    lines = []
    for j in jobs:
        lines.append(
            f"{j.get('job_id', '?')} [{j.get('status', '?')}] "
            f"runtime={j.get('runtime', '?')} exit={j.get('exit_code', '-')} "
            f"session={j.get('session_id', '')}"
        )
    return "\n".join(lines)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
