"""Background poller: sync running/review/ci_check tasks with dispatch job status."""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from hub import config, services
from hub import repository as repo
from hub.db import log_activity
from hub.integrations.registry import plugins

log = logging.getLogger("hub")

POLL_INTERVAL = 30  # seconds


async def _poll_running_tasks(app: FastAPI) -> None:
    """Background task: sync running/review/fix_requested tasks with dispatch job status."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            db = app.state.db

            rows = await repo.list_running_dispatchable(db)
            for row in rows:
                task = dict(row)
                job = plugins.dispatch.get_job(task["job_id"])
                if not job:
                    continue
                job_status = job.get("status")
                if job_status not in ("completed", "failed"):
                    continue

                if job_status == "failed":
                    await repo.update_task(
                        db,
                        task["id"],
                        status="failed",
                        exit_code=job.get("exit_code"),
                        result_text=job.get("result_text"),
                    )
                    await db.commit()
                    log.info(
                        "Poll: task #%d → failed (exit=%s)",
                        task["id"],
                        job.get("exit_code"),
                    )
                    await services.maybe_destroy_vast(db, task)
                    continue

                if (
                    task.get("auto_review")
                    and task.get("review_cycle", 0) < config.MAX_REVIEW_CYCLES
                ):
                    branch = task.get("branch")
                    if branch:
                        await plugins.git_ops.checkout(branch)
                        await plugins.git_ops.auto_commit(
                            task["id"], title=task.get("title", "")
                        )
                        squashed = await plugins.git_ops.squash_branch(
                            task["id"],
                            task.get("title", ""),
                            branch,
                        )
                        await plugins.git_ops.push_branch(branch, force=squashed)
                        if not task.get("pr_number"):
                            pr_num = await plugins.git_ops.create_pr(
                                task["id"],
                                task["title"],
                                task.get("description", ""),
                                branch,
                            )
                            if pr_num:
                                await repo.update_task(db, task["id"], pr_number=pr_num)
                                task["pr_number"] = pr_num
                    await repo.update_task(
                        db,
                        task["id"],
                        status="ci_check",
                        exit_code=job.get("exit_code"),
                        result_text=job.get("result_text"),
                    )
                    await db.commit()
                    log.info("Poll: task #%d → ci_check (waiting for CI)", task["id"])
                else:
                    has_done = await repo.has_done_updates(db, task["id"])
                    next_status = "completed" if has_done else "pending_report"
                    await repo.update_task(
                        db,
                        task["id"],
                        status=next_status,
                        exit_code=job.get("exit_code"),
                        result_text=job.get("result_text"),
                    )
                    await db.commit()
                    log.info(
                        "Poll: task #%d → %s (exit=%s)",
                        task["id"],
                        next_status,
                        job.get("exit_code"),
                    )
                    await services.maybe_destroy_vast(db, task)

            review_rows = await repo.list_review_tasks(db)
            for row in review_rows:
                task = dict(row)
                job = plugins.dispatch.get_job(task["review_job_id"])
                if not job:
                    continue
                job_status = job.get("status")
                if job_status not in ("completed", "failed"):
                    continue

                updates_rows = await repo.get_task_updates(db, task["id"])
                updates_list = [dict(r) for r in updates_rows]
                has_alert = any(
                    u.get("kind") == "alert"
                    and "cycle limit" in u.get("content", "").lower()
                    for u in updates_list
                )
                has_arbitration = any(
                    u.get("kind") == "arbitration" for u in updates_list
                )

                if job_status == "failed":
                    if has_alert or has_arbitration:
                        await repo.update_task(db, task["id"], status="needs_decision")
                        await db.commit()
                        await repo.add_task_update(
                            db,
                            task["id"],
                            "hub",
                            "alert",
                            f"Review/arbiter job failed (exit={job.get('exit_code')}). Manual decision required.",
                        )
                        await db.commit()
                        log.info(
                            "Poll: review/arbiter job failed for task #%d after cycle limit → needs_decision",
                            task["id"],
                        )
                        await services.maybe_destroy_vast(db, task)
                    else:
                        await repo.update_task(db, task["id"], status="completed")
                        await db.commit()
                        log.info(
                            "Poll: review job failed for task #%d, marking completed",
                            task["id"],
                        )
                        await services.maybe_destroy_vast(db, task)
                    continue

                verdict = services.extract_review_verdict(
                    task["id"], task["review_job_id"], updates_list
                )

                if has_arbitration:
                    await repo.update_task(db, task["id"], status="needs_decision")
                    await db.commit()
                    log.info("Poll: task #%d arbiter done → needs_decision", task["id"])
                    await services.maybe_destroy_vast(db, task)
                    continue

                if verdict == "approved":
                    pr_num = task.get("pr_number")
                    branch = task.get("branch")
                    merged = False
                    if pr_num:
                        ci = await plugins.git_ops.check_pr_ci(pr_num)
                        if ci == "pass":
                            merged = await plugins.git_ops.merge_pr(
                                pr_num, task["id"], task["title"]
                            )
                            if merged:
                                await plugins.git_ops.pull_main()
                                if branch:
                                    await plugins.git_ops.delete_branch(branch)
                                log.info(
                                    "Poll: task #%d PR #%d merged on GitHub",
                                    task["id"],
                                    pr_num,
                                )
                        elif ci == "fail":
                            log.warning(
                                "Poll: task #%d CI failed on PR #%d", task["id"], pr_num
                            )
                            await repo.add_task_update(
                                db,
                                task["id"],
                                "hub",
                                "alert",
                                f"CI failed on PR #{pr_num}. Manual check required.",
                            )
                        else:
                            log.info(
                                "Poll: task #%d CI pending on PR #%d, will retry",
                                task["id"],
                                pr_num,
                            )
                            continue
                    if not merged and not pr_num:
                        log.info("Poll: task #%d approved (no PR)", task["id"])
                    await repo.update_task(db, task["id"], status="completed")
                    await db.commit()
                    log.info("Poll: task #%d review → approved", task["id"])
                    await services.maybe_destroy_vast(db, task)
                elif verdict == "changes_requested":
                    if task.get("review_cycle", 0) + 1 >= config.MAX_REVIEW_CYCLES:
                        await repo.update_task(
                            db,
                            task["id"],
                            review_cycle=task.get("review_cycle", 0) + 1,
                        )
                        await db.commit()
                        await services.dispatch_arbiter(db, task, updates_list)
                    else:
                        review_text = ""
                        for u in reversed(updates_list):
                            if u.get("kind") == "review":
                                review_text = u.get("content", "")
                                break
                        branch = task.get("branch")
                        if branch:
                            await plugins.git_ops.checkout(branch)
                        await services.dispatch_fix(db, task, review_text)
                else:
                    log.info(
                        "Poll: task #%d review job done but no clear verdict, marking completed",
                        task["id"],
                    )
                    await repo.update_task(db, task["id"], status="completed")
                    await db.commit()
                    await services.maybe_destroy_vast(db, task)

            ci_rows = await repo.list_ci_check_tasks(db)
            for row in ci_rows:
                task = dict(row)
                if not task.get("pr_number"):
                    branch = task.get("branch")
                    if branch:
                        await plugins.git_ops.push_branch(branch, force=True)
                        pr_num = await plugins.git_ops.create_pr(
                            task["id"],
                            task["title"],
                            task.get("description", ""),
                            branch,
                        )
                        if pr_num:
                            await repo.update_task(db, task["id"], pr_number=pr_num)
                            task["pr_number"] = pr_num
                            await db.commit()
                            log.info(
                                "Poll: task #%d created PR #%d (was missing)",
                                task["id"],
                                pr_num,
                            )
                    if not task.get("pr_number"):
                        continue
                ci = await plugins.git_ops.check_pr_ci(task["pr_number"])
                if ci == "pending":
                    continue
                if ci == "pass":
                    log.info(
                        "Poll: task #%d CI passed on PR #%s, dispatching review",
                        task["id"],
                        task.get("pr_number"),
                    )
                    await services.dispatch_review(db, task)
                elif ci == "fail":
                    ci_fix_cycle = task.get("ci_fix_cycle", 0)
                    if ci_fix_cycle < config.MAX_CI_FIX_CYCLES:
                        ci_details = await plugins.git_ops.get_ci_failure_logs(
                            task["pr_number"],
                            task.get("branch", ""),
                        )
                        log.info(
                            "Poll: task #%d CI failed (cycle %d/%d), dispatching CI fix",
                            task["id"],
                            ci_fix_cycle + 1,
                            config.MAX_CI_FIX_CYCLES,
                        )
                        await services.dispatch_ci_fix(db, task, ci_details)
                    else:
                        await repo.add_task_update(
                            db,
                            task["id"],
                            "hub",
                            "alert",
                            f"CI fix cycle limit reached ({ci_fix_cycle}/{config.MAX_CI_FIX_CYCLES}). "
                            "Manual intervention required.",
                        )
                        await repo.update_task(db, task["id"], status="needs_decision")
                        await db.commit()
                        log.info(
                            "Poll: task #%d CI fix cycle limit → needs_decision",
                            task["id"],
                        )
                        await services.maybe_destroy_vast(db, task)

            stale_rows = await repo.list_stale_running(
                db, config.STALE_THRESHOLD_MINUTES
            )
            for row in stale_rows:
                task = dict(row)
                if await repo.has_stale_alert(db, task["id"]):
                    continue
                await repo.add_task_update(
                    db,
                    task["id"],
                    "hub",
                    "alert",
                    f"Task stale: no updates for {config.STALE_THRESHOLD_MINUTES}+ minutes.",
                )
                await db.commit()
                await log_activity(
                    db,
                    "task_stale",
                    f"Task #{task['id']} has no updates for {config.STALE_THRESHOLD_MINUTES}+ min",
                )
                log.warning(
                    "Poll: task #%d is stale (no updates for %d+ min)",
                    task["id"],
                    config.STALE_THRESHOLD_MINUTES,
                )

        except Exception:
            log.exception("Poll error")


def start_poller(app: FastAPI) -> asyncio.Task[None]:
    """Create and return the background poller task."""
    task = asyncio.create_task(_poll_running_tasks(app))
    log.info("Background poller started (every %ds)", POLL_INTERVAL)
    return task
