"""Readiness calculator.

Deterministic, non-LLM scoring of how ready a task is to be picked up by
a Developer agent. The score is a flat 0..100 number computed from:

- the DoR evaluation (#36) — penalty per failed required/optional check;
- the explicit risk list on the task — penalty per risk by severity.

Defaults were chosen so that an entirely empty feature task lands near
zero and a fully-described feature with no risks is exactly 100. The
constants live in ``ReadinessConfig`` so the gate can be tuned later
without touching the algorithm.

Recommendations are intentionally produced by a separate engine (#38).
This module exposes only the score, the DoR result echoed back, and the
risk list — Recommendations consumes that output to suggest actions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from hub import repository as repo
from hub.db import deserialize_risks
from hub.models import (
    ReadinessReport,
    RiskSeverity,
    TaskRisk,
)
from hub.services.dor import DoREvaluation, evaluate_dor

log = logging.getLogger("hub.services.readiness")


@dataclass(frozen=True)
class ReadinessConfig:
    """Tunable scoring parameters. Defaults documented in module docstring."""

    base: int = 100
    penalty_required: int = 10
    penalty_optional: int = 2
    risk_penalties: dict[RiskSeverity, int] = field(
        default_factory=lambda: {
            RiskSeverity.low: 1,
            RiskSeverity.medium: 4,
            RiskSeverity.high: 8,
        }
    )

    def penalty_for_risk(self, severity: RiskSeverity) -> int:
        return self.risk_penalties.get(severity, 0)


DEFAULT_CONFIG = ReadinessConfig()


@dataclass(frozen=True)
class ScoreComponent:
    """One line of the score breakdown — used in the ``explain`` payload."""

    field: str
    delta: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "delta": self.delta, "reason": self.reason}


def calculate_score_from_data(
    *,
    dor: DoREvaluation,
    risks: list[TaskRisk],
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> tuple[int, list[ScoreComponent]]:
    """Pure scoring: DoR + risks → (score, components).

    - Each failed REQUIRED check costs ``penalty_required``.
    - Each failed OPTIONAL check (in DOR_CHECK_KEYS but not required for
      this work_type) costs ``penalty_optional`` — small nudge, not a block.
    - Each risk costs by severity.
    - Final score is clamped to [0, 100].

    Returns components in the order they were applied so that ``explain``
    mirrors the calculation top-down.
    """
    score = config.base
    components: list[ScoreComponent] = []

    for check in dor.checks:
        if check.passed:
            continue
        is_required = check.key in dor.required
        penalty = config.penalty_required if is_required else config.penalty_optional
        score -= penalty
        components.append(
            ScoreComponent(
                field=check.key,
                delta=-penalty,
                reason=(
                    f"DoR required check '{check.key}' failed"
                    if is_required
                    else f"DoR optional check '{check.key}' failed"
                ),
            )
        )

    for idx, risk in enumerate(risks):
        penalty = config.penalty_for_risk(risk.severity)
        if penalty <= 0:
            continue
        score -= penalty
        components.append(
            ScoreComponent(
                field="risks",
                delta=-penalty,
                reason=(
                    f"risk #{idx + 1} {risk.kind.value} "
                    f"(severity={risk.severity.value})"
                ),
            )
        )

    score = max(0, min(config.base, score))
    return score, components


def _parse_risks_from_row(raw: str | None) -> list[TaskRisk]:
    """Validate JSON-stored risks via Pydantic, drop malformed entries."""
    out: list[TaskRisk] = []
    for item in deserialize_risks(raw):
        try:
            out.append(TaskRisk(**item))
        except (ValidationError, TypeError) as exc:
            log.warning("dropping malformed risk %r: %s", item, exc)
    return out


async def calculate_readiness(
    db,
    task_id: int,
    *,
    explain: bool = False,
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> ReadinessReport:
    """End-to-end readiness for a task.

    Loads the task and ACs through the repository, runs DoR (#36),
    parses persisted risks, computes the score, and returns a
    ReadinessReport. Recommendations are left empty here — the
    Recommendations engine (#38) populates them.
    """
    dor = await evaluate_dor(db, task_id)
    row = await repo.get_task(db, task_id)
    risks_raw = row["risks"] if row is not None and "risks" in row.keys() else None
    risks = _parse_risks_from_row(risks_raw)

    score, components = calculate_score_from_data(dor=dor, risks=risks, config=config)
    return ReadinessReport(
        score=score,
        dor_passed=dor.passed,
        dor_checks=dor.checks,
        risks=risks,
        recommendations=[],
        explain=[c.to_dict() for c in components] if explain else None,
    )


__all__ = [
    "DEFAULT_CONFIG",
    "ReadinessConfig",
    "ScoreComponent",
    "calculate_readiness",
    "calculate_score_from_data",
]
