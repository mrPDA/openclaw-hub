"""notesforllm integration via n4l CLI bridge."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from hub.config import N4L_BIN, N4L_SPACE_ID

log = logging.getLogger(__name__)


async def _n4l(tool: str, payload: dict[str, Any] | None = None, timeout: float = 15) -> Any:
    if payload is None:
        payload = {}
    input_json = json.dumps(payload)
    try:
        proc = await asyncio.create_subprocess_exec(
            N4L_BIN, tool,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError):
        log.debug("n4l binary not found at %s", N4L_BIN)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_json.encode()), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("n4l %s timed out", tool)
        return None
    if proc.returncode != 0:
        log.warning("n4l %s failed: %s", tool, stderr.decode(errors="replace"))
        return None
    raw = stdout.decode(errors="replace").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


async def list_spaces() -> list[dict[str, Any]]:
    result = await _n4l("spaces_list")
    if isinstance(result, list):
        return result
    return []


async def recent_decisions(space_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    sid = space_id or N4L_SPACE_ID
    if not sid:
        spaces = await list_spaces()
        if spaces:
            sid = spaces[0].get("id", "")
    if not sid:
        return []
    result = await _n4l("notes_query", {
        "space_id": sid,
        "type": "decision",
        "limit": limit,
        "sort": "newest",
    })
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "pages" in result:
        return result["pages"]
    return []


async def recent_checkpoints(space_id: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    sid = space_id or N4L_SPACE_ID
    if not sid:
        return []
    result = await _n4l("notes_query", {
        "space_id": sid,
        "workflow": "checkpoint",
        "limit": limit,
        "sort": "newest",
    })
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "pages" in result:
        return result["pages"]
    return []


async def task_timeline(space_id: str, task_id: str) -> list[dict[str, Any]]:
    result = await _n4l("notes_task_timeline", {
        "space_id": space_id,
        "task_id": task_id,
    })
    if isinstance(result, list):
        return result
    return []


async def resume_context(space_id: str, task_id: str) -> dict[str, Any] | None:
    result = await _n4l("notes_resume_context", {
        "space_id": space_id,
        "task_id": task_id,
    })
    if isinstance(result, dict):
        return result
    return None
