"""oc-dev-dispatch integration — read job states, submit tasks, build enriched messages."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from hub.config import DISPATCH_BIN, DISPATCH_JOBS_DIR, DISPATCH_LOGS_DIR

log = logging.getLogger(__name__)


def _read_job_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    if not DISPATCH_JOBS_DIR.is_dir():
        return []
    files = sorted(DISPATCH_JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    jobs: list[dict[str, Any]] = []
    for f in files[:limit]:
        data = _read_job_file(f)
        if data:
            jobs.append(data)
    return jobs


def get_job(job_id: str) -> dict[str, Any] | None:
    path = DISPATCH_JOBS_DIR / f"{job_id}.json"
    if path.exists():
        return _read_job_file(path)
    return None


def job_log_tail(job_id: str, max_lines: int = 60) -> list[str]:
    path = DISPATCH_LOGS_DIR / f"{job_id}.log"
    if not path.exists():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
        return lines[-max_lines:]
    except Exception:
        return []


def build_enriched_message(
    title: str,
    description: str,
    updates: list[dict[str, Any]] | None = None,
) -> str:
    """Build a dispatch message that includes original task context plus Q&A history."""
    parts = [f"Задача: {title}"]
    if description:
        parts.append(description)

    if updates:
        context_lines: list[str] = []
        for u in updates:
            kind = u.get("kind", "status")
            agent = u.get("agent", "")
            content = u.get("content", "")
            prefix = agent if agent else "Агент"
            if kind == "question":
                context_lines.append(f"[вопрос] {prefix}: {content}")
            elif kind == "answer":
                context_lines.append(f"[ответ] Человек: {content}")
            elif kind in ("status", "report", "blocker", "done"):
                context_lines.append(f"[{kind}] {prefix}: {content}")

        if context_lines:
            parts.append("\n--- Контекст предыдущей работы ---")
            parts.extend(context_lines)
            parts.append("\nПродолжи выполнение задачи с учётом контекста выше.")

    return "\n\n".join(parts)


def build_review_message(
    task_id: int,
    title: str,
    description: str,
    review_cycle: int,
    max_cycles: int,
) -> str:
    """Build a dispatch message for the code-reviewer agent."""
    return (
        f"Проведи code review задачи #{task_id}: {title}\n\n"
        f"Описание задачи:\n{description}\n\n"
        f"Цикл ревью: {review_cycle + 1}/{max_cycles}\n\n"
        "Инструкции:\n"
        "1. Прочитай git diff (git diff HEAD~1 или git log --oneline -3 чтобы найти нужные коммиты)\n"
        "2. Оцени: корректность логики, наличие тестов, стиль кода, обратная совместимость\n"
        "3. Напиши результат ревью через:\n"
        f"   oc-hub update {task_id} --kind review --agent code-reviewer --message \"<твой отчёт>\"\n"
        "4. В ПОСЛЕДНЕЙ строке отчёта напиши ровно одно слово: APPROVED или CHANGES_REQUESTED\n"
    )


def build_fix_message(
    task_id: int,
    title: str,
    description: str,
    review_comments: str,
    review_cycle: int,
    max_cycles: int,
) -> str:
    """Build a dispatch message to send back to the developer with review findings."""
    return (
        f"Задача #{task_id}: {title}\n\n"
        f"{description}\n\n"
        f"--- Замечания ревьюера (цикл {review_cycle}/{max_cycles}) ---\n"
        f"{review_comments}\n\n"
        "Исправь указанные замечания. После исправления запусти тесты (uv run pytest tests/ -x -q).\n"
        f"По завершении обнови статус: oc-hub update {task_id} --kind done --message \"<что исправлено>\"\n"
    )


def build_arbiter_message(
    task_id: int,
    title: str,
    description: str,
    review_history: list[dict[str, Any]],
    review_cycle: int,
    max_cycles: int,
) -> str:
    """Build a dispatch message for the arbiter agent (Claude Sonnet)."""
    parts = [
        f"АРБИТРАЖ задачи #{task_id}: {title}",
        f"\nОписание задачи:\n{description}",
        f"\nЦиклы ревью исчерпаны ({review_cycle}/{max_cycles}). "
        "Разработчик и ревьюер не смогли прийти к согласию.",
    ]

    if review_history:
        parts.append("\n--- История ревью ---")
        for u in review_history:
            kind = u.get("kind", "status")
            agent = u.get("agent", "")
            content = u.get("content", "")
            prefix = agent if agent else "Agent"
            parts.append(f"[{kind}] {prefix}: {content}")

    parts.append(
        "\nИнструкции арбитру:\n"
        "1. Прочитай git diff и текущее состояние кода\n"
        "2. Проанализируй каждое замечание ревьюера: какие критичны, какие второстепенны, какие ложные\n"
        "3. Оцени качество исправлений разработчика\n"
        "4. Напиши нейтральный отчёт с рекомендацией: принять как есть или доработать (и что именно)\n"
        f"5. Запиши результат: oc-hub update {task_id} --kind arbitration --agent arbiter --message \"<отчёт>\"\n"
    )

    return "\n\n".join(parts)


async def submit_task(message: str, runtime: str = "auto", repo_root: str | None = None) -> dict[str, Any]:
    cmd = [DISPATCH_BIN, "submit", "--message", message, "--runtime", runtime, "--wait-sec", "0"]
    if repo_root:
        cmd.extend(["--repo-root", repo_root])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError):
        log.warning("oc-dev-dispatch binary not found at %s", DISPATCH_BIN)
        return {"error": f"dispatch binary not found: {DISPATCH_BIN}"}
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    raw = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        log.warning("oc-dev-dispatch submit failed: %s", stderr.decode(errors="replace"))
        return {"error": stderr.decode(errors="replace"), "exit_code": proc.returncode}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw, "exit_code": proc.returncode}


async def classify_task(message: str, repo_root: str | None = None) -> dict[str, Any]:
    cmd = [DISPATCH_BIN, "classify", "--message", message]
    if repo_root:
        cmd.extend(["--repo-root", repo_root])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError):
        return {"error": f"dispatch binary not found: {DISPATCH_BIN}"}
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    raw = stdout.decode(errors="replace").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
