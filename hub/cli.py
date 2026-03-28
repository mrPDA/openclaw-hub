#!/usr/bin/env python3
"""oc-hub — CLI for agents on Pi to interact with OpenClaw Hub."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

HUB_URL = os.environ.get("OPENCLAW_HUB_URL", "http://127.0.0.1:8080")


def _api(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{HUB_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace") if e.fp else ""
        print(f"HTTP {e.code}: {body_text}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection error: {e.reason}\nIs openclaw-hub running at {HUB_URL}?", file=sys.stderr)
        sys.exit(1)


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_task_short(t: dict[str, Any]) -> None:
    src = f" [{t.get('source', 'human')}]" if t.get("source") == "agent" else ""
    print(f"#{t['id']} [{t['status']}] ({t.get('runtime', 'auto')}){src} {t['title']}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_task(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "title": args.title,
        "description": args.description or "",
        "runtime": args.runtime,
        "source": "human",
        "run_immediately": args.run,
        "auto_review": not args.no_review,
    }
    result = _api("POST", "/api/tasks", body)
    _print_json(result)
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "title": args.title,
        "description": args.description or "",
        "source": "agent",
        "agent": args.agent or "",
        "rationale": args.rationale or "",
    }
    result = _api("POST", "/api/tasks", body)
    _print_json(result)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "comment": args.comment or "",
        "run": args.run,
    }
    if args.runtime:
        body["runtime"] = args.runtime
    result = _api("POST", f"/api/tasks/{args.task_id}/approve", body)
    _print_json(result)
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"comment": args.comment or ""}
    result = _api("POST", f"/api/tasks/{args.task_id}/reject", body)
    _print_json(result)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    if args.runtime:
        body["runtime"] = args.runtime
    result = _api("POST", f"/api/tasks/{args.task_id}/start", body)
    _print_json(result)
    return 0


def cmd_question(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "agent": args.agent or "",
        "question": args.message,
    }
    result = _api("POST", f"/api/tasks/{args.task_id}/question", body)
    _print_json(result)
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "answer": args.message,
        "resume": not args.no_resume,
    }
    result = _api("POST", f"/api/tasks/{args.task_id}/answer", body)
    _print_json(result)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    result = _api("GET", f"/api/tasks/{args.task_id}")
    _print_json(result)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    params = f"?limit={args.limit}"
    if args.status:
        params += f"&status={args.status}"
    result = _api("GET", f"/api/tasks{params}")
    for t in result:
        _print_task_short(t)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    result = _api("POST", f"/api/tasks/{args.task_id}/updates", {
        "agent": args.agent or "",
        "kind": args.kind,
        "content": args.message,
    })
    _print_json(result)
    return 0


def cmd_updates(args: argparse.Namespace) -> int:
    result = _api("GET", f"/api/tasks/{args.task_id}/updates")
    for u in result:
        print(f"[{u['created_at']}] ({u['kind']}) {u.get('agent','')}: {u['content']}")
    return 0


def cmd_proposals(args: argparse.Namespace) -> int:
    params = f"?status={args.status}" if args.status else ""
    result = _api("GET", f"/api/tasks?status={'draft' if not args.status or args.status == 'pending' else args.status}&limit=50")
    for t in result:
        if t.get("source") == "agent":
            _print_task_short(t)
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    action = "accept" if args.accept else "rework"
    body: dict[str, Any] = {
        "action": action,
        "instructions": args.message or "",
    }
    result = _api("POST", f"/api/tasks/{args.task_id}/decide", body)
    _print_json(result)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    result = _api("GET", "/api/dashboard")
    _print_json(result)
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oc-hub", description="CLI for OpenClaw Hub")
    sub = parser.add_subparsers(dest="command", required=True)

    # task — create a task (open by default, or running with --run)
    p_task = sub.add_parser("task", help="Create a task (open by default, --run to dispatch immediately)")
    p_task.add_argument("--title", required=True)
    p_task.add_argument("--description", default="")
    p_task.add_argument("--runtime", choices=["auto", "openrouter", "vast"], default="auto")
    p_task.add_argument("--run", action="store_true", help="Dispatch immediately")
    p_task.add_argument("--no-review", action="store_true", help="Disable automatic code review for this task")
    p_task.set_defaults(func=cmd_task)

    # propose — agent proposes a task (creates draft)
    p_propose = sub.add_parser("propose", help="Propose a task for human approval (creates draft)")
    p_propose.add_argument("--title", required=True)
    p_propose.add_argument("--description", default="")
    p_propose.add_argument("--agent", default="")
    p_propose.add_argument("--rationale", default="")
    p_propose.set_defaults(func=cmd_propose)

    # approve — approve a draft task
    p_approve = sub.add_parser("approve", help="Approve a draft task")
    p_approve.add_argument("task_id", type=int)
    p_approve.add_argument("--comment", default="")
    p_approve.add_argument("--run", action="store_true", help="Also dispatch immediately")
    p_approve.add_argument("--runtime", choices=["auto", "openrouter", "vast"], default=None)
    p_approve.set_defaults(func=cmd_approve)

    # reject — reject a draft task
    p_reject = sub.add_parser("reject", help="Reject a draft task")
    p_reject.add_argument("task_id", type=int)
    p_reject.add_argument("--comment", default="")
    p_reject.set_defaults(func=cmd_reject)

    # start — dispatch an open task
    p_start = sub.add_parser("start", help="Dispatch an open task")
    p_start.add_argument("task_id", type=int)
    p_start.add_argument("--runtime", choices=["auto", "openrouter", "vast"], default=None)
    p_start.set_defaults(func=cmd_start)

    # question — agent asks a question (sets needs_info)
    p_question = sub.add_parser("question", help="Agent asks a question on a running task")
    p_question.add_argument("task_id", type=int)
    p_question.add_argument("--message", required=True, help="The question text")
    p_question.add_argument("--agent", default="", help="Agent name")
    p_question.set_defaults(func=cmd_question)

    # answer — human answers a question (re-dispatches by default)
    p_answer = sub.add_parser("answer", help="Answer agent question and re-dispatch")
    p_answer.add_argument("task_id", type=int)
    p_answer.add_argument("--message", required=True, help="The answer text")
    p_answer.add_argument("--no-resume", action="store_true", help="Don't re-dispatch, just save answer")
    p_answer.set_defaults(func=cmd_answer)

    # status — get task details
    p_status = sub.add_parser("status", help="Get task status")
    p_status.add_argument("task_id", type=int)
    p_status.set_defaults(func=cmd_status)

    # list — list tasks
    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("--status", choices=["draft", "open", "running", "needs_info", "review", "fix_requested", "needs_decision", "completed", "failed", "rejected"], default=None)
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    # update — add status update to a task
    p_update = sub.add_parser("update", help="Add a status update or report to a task")
    p_update.add_argument("task_id", type=int)
    p_update.add_argument("--message", required=True, help="Update content")
    p_update.add_argument("--agent", default="", help="Agent name")
    p_update.add_argument("--kind", default="status", choices=["status", "report", "blocker", "done", "review", "arbitration"], help="Update type")
    p_update.set_defaults(func=cmd_update)

    # updates — list updates
    p_updates = sub.add_parser("updates", help="List updates for a task")
    p_updates.add_argument("task_id", type=int)
    p_updates.set_defaults(func=cmd_updates)

    # proposals — list agent proposals (backward compat)
    p_proposals = sub.add_parser("proposals", help="List agent proposals (draft tasks)")
    p_proposals.add_argument("--status", choices=["pending", "approved", "rejected"], default=None)
    p_proposals.set_defaults(func=cmd_proposals)

    # decide — human decision after arbiter
    p_decide = sub.add_parser("decide", help="Accept or rework a task after arbiter review")
    p_decide.add_argument("task_id", type=int)
    group = p_decide.add_mutually_exclusive_group(required=True)
    group.add_argument("--accept", action="store_true", help="Accept task as completed")
    group.add_argument("--rework", action="store_true", help="Send back for rework")
    p_decide.add_argument("--message", default="", help="Instructions for rework")
    p_decide.set_defaults(func=cmd_decide)

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Get dashboard data (JSON)")
    p_dash.set_defaults(func=cmd_dashboard)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
