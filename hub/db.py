from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from hub.config import HUB_DB_PATH

log = logging.getLogger("hub.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'open',
    runtime         TEXT    NOT NULL DEFAULT 'auto',
    source          TEXT    NOT NULL DEFAULT 'human',
    assigned_agent  TEXT    NOT NULL DEFAULT '',
    rationale       TEXT    NOT NULL DEFAULT '',
    job_id          TEXT,
    exit_code       INTEGER,
    result_text     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_updates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id),
    agent      TEXT    NOT NULL DEFAULT '',
    kind       TEXT    NOT NULL DEFAULT 'status',
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activity_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    summary   TEXT NOT NULL,
    detail    TEXT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_MIGRATIONS: list[tuple[str, str]] = [
    (
        "add_source_column",
        "ALTER TABLE tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'human'",
    ),
    (
        "add_assigned_agent_column",
        "ALTER TABLE tasks ADD COLUMN assigned_agent TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_rationale_column",
        "ALTER TABLE tasks ADD COLUMN rationale TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_review_cycle_column",
        "ALTER TABLE tasks ADD COLUMN review_cycle INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "add_auto_review_column",
        "ALTER TABLE tasks ADD COLUMN auto_review INTEGER NOT NULL DEFAULT 1",
    ),
    ("add_review_job_id_column", "ALTER TABLE tasks ADD COLUMN review_job_id TEXT"),
    ("add_branch_column", "ALTER TABLE tasks ADD COLUMN branch TEXT"),
    ("add_pr_number_column", "ALTER TABLE tasks ADD COLUMN pr_number INTEGER"),
    (
        "add_ci_fix_cycle_column",
        "ALTER TABLE tasks ADD COLUMN ci_fix_cycle INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "add_task_type_column",
        "ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'task'",
    ),
    (
        "add_parent_id_column",
        "ALTER TABLE tasks ADD COLUMN parent_id INTEGER REFERENCES tasks(id)",
    ),
    (
        "add_position_column",
        "ALTER TABLE tasks ADD COLUMN position INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "add_priority_column",
        "ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium'",
    ),
    # Indexes for frequent queries
    (
        "idx_tasks_parent_id",
        "CREATE INDEX IF NOT EXISTS idx_tasks_parent_id ON tasks(parent_id)",
    ),
    (
        "idx_tasks_status",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    ),
    (
        "idx_tasks_type_status",
        "CREATE INDEX IF NOT EXISTS idx_tasks_type_status ON tasks(task_type, status)",
    ),
    (
        "idx_task_updates_task_id",
        "CREATE INDEX IF NOT EXISTS idx_task_updates_task_id ON task_updates(task_id)",
    ),
    # Structured task form (Kanban DoR) — see Epic #32.
    # Nullable / empty defaults so existing rows stay valid without backfill.
    (
        "add_work_type_column",
        "ALTER TABLE tasks ADD COLUMN work_type TEXT NOT NULL DEFAULT 'feature'",
    ),
    (
        "add_class_of_service_column",
        "ALTER TABLE tasks ADD COLUMN class_of_service TEXT NOT NULL DEFAULT 'standard'",
    ),
    ("add_size_column", "ALTER TABLE tasks ADD COLUMN size TEXT"),
    ("add_wip_tag_column", "ALTER TABLE tasks ADD COLUMN wip_tag TEXT"),
    ("add_due_date_column", "ALTER TABLE tasks ADD COLUMN due_date TEXT"),
    (
        "add_user_story_column",
        "ALTER TABLE tasks ADD COLUMN user_story TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_problem_statement_column",
        "ALTER TABLE tasks ADD COLUMN problem_statement TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_business_value_column",
        "ALTER TABLE tasks ADD COLUMN business_value TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_scope_in_column",
        "ALTER TABLE tasks ADD COLUMN scope_in TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_scope_out_column",
        "ALTER TABLE tasks ADD COLUMN scope_out TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_affected_areas_column",
        "ALTER TABLE tasks ADD COLUMN affected_areas TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_technical_hints_column",
        "ALTER TABLE tasks ADD COLUMN technical_hints TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_constraints_column",
        "ALTER TABLE tasks ADD COLUMN constraints TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_assumptions_column",
        "ALTER TABLE tasks ADD COLUMN assumptions TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_validation_commands_column",
        "ALTER TABLE tasks ADD COLUMN validation_commands TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_out_of_scope_for_review_column",
        "ALTER TABLE tasks ADD COLUMN out_of_scope_for_review TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_risks_column",
        "ALTER TABLE tasks ADD COLUMN risks TEXT NOT NULL DEFAULT '[]'",
    ),
    ("add_readiness_score_column", "ALTER TABLE tasks ADD COLUMN readiness_score INTEGER"),
    ("add_dor_passed_column", "ALTER TABLE tasks ADD COLUMN dor_passed INTEGER"),
    ("add_ready_at_column", "ALTER TABLE tasks ADD COLUMN ready_at TEXT"),
    ("add_started_at_column", "ALTER TABLE tasks ADD COLUMN started_at TEXT"),
    ("add_completed_at_column", "ALTER TABLE tasks ADD COLUMN completed_at TEXT"),
    (
        "create_acceptance_criteria_table",
        """CREATE TABLE IF NOT EXISTS acceptance_criteria (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            ac_id           TEXT    NOT NULL,
            given           TEXT    NOT NULL,
            when_clause     TEXT    NOT NULL,
            then_clause     TEXT    NOT NULL,
            verifiable_by   TEXT    NOT NULL,
            test_ref        TEXT,
            position        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (task_id, ac_id)
        )""",
    ),
    (
        "idx_acceptance_criteria_task_id",
        "CREATE INDEX IF NOT EXISTS idx_acceptance_criteria_task_id ON acceptance_criteria(task_id)",
    ),
]


def serialize_str_list(items: list[str] | None) -> str:
    """Serialize list[str] for storage in a TEXT column.

    None or empty list -> '[]' for stable defaults and predictable diffs.
    """
    if not items:
        return "[]"
    return json.dumps(list(items), ensure_ascii=False)


def deserialize_str_list(raw: str | None) -> list[str]:
    """Deserialize list[str] from a TEXT column.

    Returns [] for None, empty string, malformed JSON, or non-list payloads —
    callers can treat the column as "always a list" without guarding.
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("deserialize_str_list: invalid JSON %r, returning []", raw)
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def serialize_risks(risks: list[dict[str, Any]] | None) -> str:
    """Serialize list[dict] (TaskRisk payloads) for storage."""
    if not risks:
        return "[]"
    return json.dumps(list(risks), ensure_ascii=False)


def deserialize_risks(raw: str | None) -> list[dict[str, Any]]:
    """Deserialize list[dict] from TEXT column. Drops non-dict items."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("deserialize_risks: invalid JSON %r, returning []", raw)
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table via PRAGMA table_info."""
    rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in rows)


async def _migrate(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
    )
    applied = {
        row[0] for row in await db.execute_fetchall("SELECT name FROM _migrations")
    }
    for name, sql in _MIGRATIONS:
        if name not in applied:
            if sql.startswith("ALTER TABLE") and "ADD COLUMN" in sql:
                parts = sql.split()
                table = parts[2]
                col = parts[5]
                if await _column_exists(db, table, col):
                    await db.execute(
                        "INSERT OR IGNORE INTO _migrations (name) VALUES (?)",
                        (name,),
                    )
                    continue
            try:
                await db.execute(sql)
            except Exception as exc:
                log.warning("Migration %s failed: %s", name, exc)
            await db.execute(
                "INSERT OR IGNORE INTO _migrations (name) VALUES (?)", (name,)
            )
    await db.commit()

    if await _table_exists(db, "proposals"):
        await _migrate_proposals(db)


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    rows = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return len(rows) > 0


async def _migrate_proposals(db: aiosqlite.Connection) -> None:
    """Migrate old proposals table into tasks with source='agent'."""
    rows = await db.execute_fetchall("SELECT * FROM proposals")
    for r in rows:
        d = dict(r)
        status_map = {"pending": "draft", "approved": "open", "rejected": "rejected"}
        new_status = status_map.get(d.get("status", ""), "draft")
        existing = await db.execute_fetchall(
            "SELECT id FROM tasks WHERE title=? AND source='agent' AND description=?",
            (d["title"], d.get("description", "")),
        )
        if existing:
            continue
        await db.execute(
            "INSERT INTO tasks (title, description, status, source, assigned_agent, rationale, created_at, updated_at) "
            "VALUES (?, ?, ?, 'agent', ?, ?, ?, ?)",
            (
                d["title"],
                d.get("description", ""),
                new_status,
                d.get("agent", ""),
                d.get("rationale", ""),
                d.get("created_at", ""),
                d.get("updated_at", ""),
            ),
        )
    await db.execute("DROP TABLE IF EXISTS proposals")
    await db.commit()


async def get_db() -> aiosqlite.Connection:
    HUB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(HUB_DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(_SCHEMA)
    await _migrate(db)
    await _fix_orphaned_parents(db)
    return db


async def _fix_orphaned_parents(db: aiosqlite.Connection) -> None:
    """Nullify parent_id references that point to nonexistent tasks."""
    orphans = await db.execute_fetchall(
        "SELECT t.id FROM tasks t "
        "LEFT JOIN tasks p ON t.parent_id = p.id "
        "WHERE t.parent_id IS NOT NULL AND p.id IS NULL",
    )
    if orphans:
        ids = [r[0] for r in orphans]
        log.warning("Fixing %d orphaned parent_id references: %s", len(ids), ids)
        for oid in ids:
            await db.execute("UPDATE tasks SET parent_id=NULL WHERE id=?", (oid,))
        await db.commit()


async def validate_hierarchy(
    db: aiosqlite.Connection,
    task_type: str,
    parent_id: int | None,
) -> str | None:
    """Validate parent-child relationship. Returns error message or None if valid."""
    from hub.models import HIERARCHY_RULES, TaskType

    tt = TaskType(task_type)
    required_parent = HIERARCHY_RULES[tt]

    if tt == TaskType.epic:
        if parent_id is not None:
            return "Epics cannot have a parent"
        return None

    if required_parent is None:
        if parent_id is not None:
            return f"{task_type} cannot have a parent"
        return None

    if tt == TaskType.task and parent_id is None:
        return None

    if parent_id is None:
        return f"{task_type} requires a parent of type {required_parent.value}"

    rows = await db.execute_fetchall(
        "SELECT task_type FROM tasks WHERE id=?", (parent_id,)
    )
    if not rows:
        return f"Parent task #{parent_id} not found"
    parent_type = rows[0][0]

    if tt == TaskType.task and parent_type in (
        TaskType.feature.value,
        TaskType.epic.value,
    ):
        return None
    if parent_type != required_parent.value:
        return f"{task_type} requires parent of type {required_parent.value}, got {parent_type}"
    return None


async def get_breadcrumb(
    db: aiosqlite.Connection,
    task_id: int,
) -> list[dict[str, Any]]:
    """Walk up the parent chain and return breadcrumb list (root first)."""
    crumbs: list[dict[str, Any]] = []
    current_id: int | None = task_id
    seen: set[int] = set()
    while current_id is not None:
        if current_id in seen:
            break
        seen.add(current_id)
        rows = await db.execute_fetchall(
            "SELECT id, title, task_type, parent_id FROM tasks WHERE id=?",
            (current_id,),
        )
        if not rows:
            break
        row = dict(rows[0])
        crumbs.append(
            {
                "id": row["id"],
                "title": row["title"],
                "task_type": row["task_type"],
            }
        )
        current_id = row["parent_id"]
    crumbs.reverse()
    return crumbs


async def get_children(
    db: aiosqlite.Connection,
    task_id: int,
) -> list[dict[str, Any]]:
    """Get direct children of a task, ordered by position then id."""
    rows = await db.execute_fetchall(
        "SELECT id, title, task_type, status, priority FROM tasks "
        "WHERE parent_id=? ORDER BY position ASC, id ASC",
        (task_id,),
    )
    return [dict(r) for r in rows]


async def get_progress(
    db: aiosqlite.Connection,
    task_id: int,
) -> dict[str, int]:
    """Calculate progress for a parent task based on direct children."""
    from hub.models import ACTIVE_STATUSES

    rows = await db.execute_fetchall(
        "SELECT status FROM tasks WHERE parent_id=?", (task_id,)
    )
    total = len(rows)
    if total == 0:
        return {"total": 0, "completed": 0, "failed": 0, "active": 0, "percent": 0}

    completed = sum(1 for r in rows if r[0] == "completed")
    failed = sum(1 for r in rows if r[0] in ("failed", "rejected"))
    active = sum(1 for r in rows if r[0] in {s.value for s in ACTIVE_STATUSES})
    percent = round(completed * 100 / total) if total > 0 else 0

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "active": active,
        "percent": percent,
    }


async def build_tree(
    db: aiosqlite.Connection,
    task_id: int,
) -> dict[str, Any] | None:
    """Build a recursive tree from a task downward."""
    rows = await db.execute_fetchall(
        "SELECT id, title, task_type, status, priority, assigned_agent FROM tasks WHERE id=?",
        (task_id,),
    )
    if not rows:
        return None

    task = dict(rows[0])
    children_rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE parent_id=? ORDER BY position ASC, id ASC",
        (task_id,),
    )
    children = []
    for cr in children_rows:
        child_tree = await build_tree(db, cr[0])
        if child_tree:
            children.append(child_tree)

    progress = await get_progress(db, task_id) if children else None
    return {
        "id": task["id"],
        "title": task["title"],
        "task_type": task["task_type"],
        "status": task["status"],
        "priority": task["priority"],
        "assigned_agent": task.get("assigned_agent", ""),
        "progress": progress,
        "children": children,
    }


async def log_activity(
    db: aiosqlite.Connection, kind: str, summary: str, detail: str | None = None
) -> None:
    await db.execute(
        "INSERT INTO activity_log (kind, summary, detail) VALUES (?, ?, ?)",
        (kind, summary, detail),
    )
    await db.commit()
