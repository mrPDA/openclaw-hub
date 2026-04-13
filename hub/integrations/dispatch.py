"""oc-dev-dispatch integration — read job states, submit tasks, build enriched messages."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from hub.config import DISPATCH_BIN, DISPATCH_JOBS_DIR, DISPATCH_LOGS_DIR

log = logging.getLogger(__name__)


def _read_job_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


class DispatchIntegration:
    """Concrete dispatch plugin backed by the oc-dev-dispatch CLI binary."""

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        if not DISPATCH_JOBS_DIR.is_dir():
            return []
        files = sorted(
            DISPATCH_JOBS_DIR.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        jobs: list[dict[str, Any]] = []
        for f in files[:limit]:
            data = _read_job_file(f)
            if data:
                jobs.append(data)
        return jobs

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        path = DISPATCH_JOBS_DIR / f"{job_id}.json"
        if path.exists():
            return _read_job_file(path)
        return None

    def job_log_tail(self, job_id: str, max_lines: int = 60) -> list[str]:
        path = DISPATCH_LOGS_DIR / f"{job_id}.log"
        if not path.exists():
            return []
        try:
            lines = path.read_text(errors="replace").splitlines()
            return lines[-max_lines:]
        except Exception:
            return []

    def job_log_full(self, job_id: str) -> str:
        path = DISPATCH_LOGS_DIR / f"{job_id}.log"
        if not path.exists():
            return ""
        try:
            return path.read_text(errors="replace")
        except Exception:
            return ""

    def build_enriched_message(
        self,
        title: str,
        description: str,
        updates: list[dict[str, Any]] | None = None,
        branch: str = "",
        breadcrumb: str = "",
    ) -> str:
        parts = [f"Задача: {title}"]
        if breadcrumb:
            parts.append(f"Контекст иерархии: {breadcrumb}")
        if description:
            parts.append(description)

        if branch:
            parts.append(
                f"GIT: Ты работаешь в ветке `{branch}`.\n"
                "ЗАПРЕЩЕНО: git commit, git push, git merge, git rebase. "
                "Hub делает ВСЁ это автоматически.\n"
                "ЗАПРЕЩЕНО: write tool для перезаписи существующих файлов. "
                "Используй ТОЛЬКО edit (patch) для правки существующих файлов. "
                "write допускается ТОЛЬКО для НОВЫХ файлов.\n"
                "После правки файла проверь синтаксис: "
                "python -c \"import ast; ast.parse(open('file.py').read())\"\n"
                "ПЕРЕД ЗАВЕРШЕНИЕМ обязательно проверь:\n"
                "  1. uv run ruff check src/ tests/ --fix && uv run ruff format src/ tests/\n"
                "  2. uv run pytest tests/ -x -q\n"
                "  3. uv pip audit — если есть CVE, обнови пакет: "
                "uv lock --upgrade-package <имя_пакета>\n"
                "Если какая-то проверка падает — исправь до завершения. "
                "Hub проверяет CI автоматически."
            )

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
        self,
        task_id: int,
        title: str,
        description: str,
        review_cycle: int,
        max_cycles: int,
        branch: str = "",
        pr_number: int | None = None,
        breadcrumb: str = "",
    ) -> str:
        from hub.config import REPO_NAME

        diff_cmd = f"git diff main..{branch}" if branch else "git diff HEAD~1"
        branch_note = f"\nВетка разработчика: `{branch}`\n" if branch else ""
        pr_note = (
            f"PR: https://github.com/{REPO_NAME}/pull/{pr_number}\n"
            if pr_number
            else ""
        )
        breadcrumb_note = f"Иерархия: {breadcrumb}\n" if breadcrumb else ""
        return (
            f"ЗАДАНИЕ: Code review задачи #{task_id}: {title}\n\n"
            f"{breadcrumb_note}"
            f"Описание задачи:\n{description}\n"
            f"{branch_note}"
            f"{pr_note}\n"
            f"Цикл ревью: {review_cycle + 1}/{max_cycles}\n\n"
            "ПОРЯДОК ДЕЙСТВИЙ:\n"
            f"1. Выполни: {diff_cmd} — это весь diff задачи относительно main\n"
            "2. Оцени: корректность логики, наличие тестов, стиль кода, обратная совместимость\n"
            "3. Для каждого замечания укажи severity (high/medium/low) и обоснование\n"
            "4. ОБЯЗАТЕЛЬНО запиши результат ревью командой:\n"
            f'   oc-hub update {task_id} --kind review --agent code-reviewer --message "<твой отчёт>"\n\n'
            "ФОРМАТ ВЕРДИКТА — последняя строка отчёта ОБЯЗАНА содержать ровно одно из:\n"
            "  APPROVED\n"
            "  CHANGES_REQUESTED\n\n"
            "Пример отчёта:\n"
            f"  Проверил {diff_cmd}. Обнаружено:\n"
            "  1. [high] Не обновлён __init__.py — сломаны импорты в тестах\n"
            "  2. [low] Отсутствует docstring в новом модуле\n"
            "  CHANGES_REQUESTED\n"
        )

    def build_fix_message(
        self,
        task_id: int,
        title: str,
        description: str,
        review_comments: str,
        review_cycle: int,
        max_cycles: int,
        branch: str = "",
    ) -> str:
        branch_note = (
            f"\nВетка: `{branch}`\n"
            "ЗАПРЕЩЕНО: git commit, git push. Hub делает это автоматически.\n"
            "ЗАПРЕЩЕНО: write tool для существующих файлов. "
            "Только edit (patch).\n"
            if branch
            else ""
        )
        return (
            f"ЗАДАНИЕ: Исправить замечания ревью для задачи #{task_id}: {title}\n\n"
            f"{description}\n"
            f"{branch_note}\n"
            f"--- Замечания ревьюера (цикл {review_cycle}/{max_cycles}) ---\n"
            f"{review_comments}\n\n"
            "ПОРЯДОК ДЕЙСТВИЙ:\n"
            "1. Исправь каждое замечание ревьюера через edit (patch)\n"
            "2. Запусти тесты: uv run pytest tests/ -x -q\n"
            f'3. ОБЯЗАТЕЛЬНО обнови статус: oc-hub update {task_id} --kind done --message "<что исправлено и как>"\n'
        )

    def build_ci_fix_message(
        self,
        task_id: int,
        title: str,
        description: str,
        ci_failures: dict[str, Any],
        ci_fix_cycle: int,
        max_cycles: int,
        branch: str = "",
    ) -> str:
        failed = ci_failures.get("failed_checks", [])
        log_summary = ci_failures.get("log_summary", "")
        run_url = ci_failures.get("run_url", "")

        checks_list = (
            "\n".join(f"  - {name}" for name in failed)
            if failed
            else "  (не удалось определить)"
        )

        branch_note = (
            f"\nВетка: `{branch}` (Hub сам коммитит перед ревью — тебе коммитить не нужно).\n"
            if branch
            else ""
        )

        parts = [
            f"ЗАДАНИЕ: Исправить ошибки CI для задачи #{task_id}: {title}\n",
            f"{description}\n",
            branch_note,
            f"Цикл CI-фикса: {ci_fix_cycle + 1}/{max_cycles}\n",
            f"--- CI ПРОВАЛЕНО ---\nУпавшие проверки:\n{checks_list}\n",
        ]

        if run_url:
            parts.append(f"Ссылка на CI run: {run_url}\n")

        if log_summary:
            parts.append(f"--- Логи ошибок (обрезаны) ---\n{log_summary}\n")

        parts.append(
            "КРИТИЧЕСКИЕ ПРАВИЛА:\n"
            "- ЗАПРЕЩЕНО: git commit, git push. Hub делает это автоматически.\n"
            "- ЗАПРЕЩЕНО: write tool для существующих файлов. "
            "Используй ТОЛЬКО edit (patch). write — только для НОВЫХ файлов.\n"
            "- После правки проверь синтаксис: "
            'python -c "import ast; ast.parse(open(\'file.py\').read())"\n\n'
            "СПРАВОЧНИК ПО CI ПРОВЕРКАМ — как исправлять каждую:\n\n"
            "  Lint & Format:\n"
            "    uv run ruff check src/ tests/ --fix\n"
            "    uv run ruff format src/ tests/\n\n"
            "  Tests:\n"
            "    uv run pytest tests/ -x -q\n\n"
            "  Security checks (pip-audit / bandit / gitleaks):\n"
            "    - pip-audit (CVE в зависимостях): "
            "uv lock --upgrade-package <package_name> "
            "(например uv lock --upgrade-package aiohttp). "
            "Имя пакета и нужная версия указаны в логе pip-audit.\n"
            "    - bandit (SAST): исправь код по рекомендациям bandit.\n"
            "    - gitleaks (секреты): убери захардкоженные секреты, "
            "используй переменные окружения.\n\n"
            "  Validate commits:\n"
            "    Формат: <type>(<scope>): <description>\n"
            "    type: feat|fix|docs|style|refactor|perf|test|chore|ci\n"
            "    scope: только [a-z]+ (без цифр, дефисов, подчёркиваний)\n"
            "    Hub коммитит сам — тебе НЕ нужно коммитить.\n\n"
            "  Architecture gate:\n"
            "    uv run pytest -q tests/test_agents_architecture.py "
            "tests/test_services_architecture.py "
            "tests/test_shopping_contract_flows.py\n\n"
            "ПОРЯДОК ДЕЙСТВИЙ:\n"
            "1. Прочитай логи ошибок CI выше ВНИМАТЕЛЬНО\n"
            "2. Определи тип каждой ошибки по справочнику\n"
            "3. Исправь каждую проблему через edit (patch), НЕ через write\n"
            "4. Проверь ЛОКАЛЬНО что исправления работают\n"
            f"5. ОБЯЗАТЕЛЬНО обнови статус: oc-hub update {task_id} --kind done "
            f'--message "<какие CI ошибки исправлены и как>"\n'
        )

        return "\n".join(parts)

    def build_arbiter_message(
        self,
        task_id: int,
        title: str,
        description: str,
        review_history: list[dict[str, Any]],
        review_cycle: int,
        max_cycles: int,
        branch: str = "",
    ) -> str:
        diff_cmd = f"git diff main..{branch}" if branch else "git diff HEAD~1"
        branch_note = f"\nВетка разработчика: `{branch}`\n" if branch else ""
        parts = [
            f"АРБИТРАЖ задачи #{task_id}: {title}",
            f"\nОписание задачи:\n{description}",
            branch_note,
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
            f"1. Прочитай {diff_cmd} — полный diff задачи относительно main\n"
            "2. Проанализируй каждое замечание ревьюера: какие критичны, какие второстепенны, какие ложные\n"
            "3. Оцени качество исправлений разработчика\n"
            "4. Напиши нейтральный отчёт с рекомендацией: принять как есть или доработать (и что именно)\n"
            f'5. Запиши результат: oc-hub update {task_id} --kind arbitration --agent arbiter --message "<отчёт>"\n'
        )

        return "\n\n".join(parts)

    async def submit_task(
        self,
        message: str,
        runtime: str = "auto",
        repo_root: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        cmd = [
            DISPATCH_BIN,
            "submit",
            "--message",
            message,
            "--runtime",
            runtime,
            "--wait-sec",
            "0",
        ]
        if repo_root:
            cmd.extend(["--repo-root", repo_root])

        env = dict(os.environ)
        if agent:
            env["OPENCLAW_OPENROUTER_DEV_AGENT"] = agent
            env["OPENCLAW_VAST_DEV_AGENT"] = agent

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, PermissionError):
            log.warning("oc-dev-dispatch binary not found at %s", DISPATCH_BIN)
            return {"error": f"dispatch binary not found: {DISPATCH_BIN}"}
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        raw = stdout.decode(errors="replace").strip()
        if proc.returncode != 0:
            log.warning(
                "oc-dev-dispatch submit failed: %s", stderr.decode(errors="replace")
            )
            return {
                "error": stderr.decode(errors="replace"),
                "exit_code": proc.returncode,
            }
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw, "exit_code": proc.returncode}

    async def classify_task(
        self, message: str, repo_root: str | None = None
    ) -> dict[str, Any]:
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
