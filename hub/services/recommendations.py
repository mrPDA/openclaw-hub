"""Recommendations engine for task readiness.

Given a DoR evaluation (#36) and the readiness scoring config (#37),
this module produces a sorted, actionable list of suggestions for the
human or AI Analyst preparing the task.

Design choices:
- We do NOT generate recommendations for risks. Risks already cost
  score in the readiness calculator; suggesting "remove this risk"
  would either be dishonest (you can't wish risks away) or trivial
  ("write a better mitigation"). Mitigation quality lives at task
  authoring level, not in the engine.
- We do NOT use an LLM. Every message is a deterministic template.
  This keeps recommendations cheap, repeatable, and reviewable.
- ``expected_score_delta`` mirrors the ReadinessConfig penalty so the
  numbers shown to a user actually match what the score would become
  after fixing the field.
"""

from __future__ import annotations

from typing import Any

from hub.models import (
    DoRCheckItem,
    Recommendation,
    RecommendationSeverity,
)
from hub.services.dor import DoREvaluation, evaluate_dor
from hub import repository as repo
from hub.models import ReadinessReport
from hub.services.readiness import (
    DEFAULT_CONFIG,
    ReadinessConfig,
    calculate_score_from_data,
    parse_risks_from_row,
)

# Static templates per check key. ``field`` is the task field a user
# would edit to satisfy the check; ``minutes`` is a rough effort estimate
# used purely as a hint, not a scheduling input.
CHECK_RECOMMENDATIONS: dict[str, dict[str, Any]] = {
    "has_user_story": {
        "field": "user_story",
        "message": "Add a user story in the form: 'As a <role>, I want <action>, so that <value>.'",
        "minutes": 5,
    },
    "has_problem_statement": {
        "field": "problem_statement",
        "message": "Describe the problem this task solves and why it matters now.",
        "minutes": 5,
    },
    "has_business_value": {
        "field": "business_value",
        "message": "Specify the expected business value (metric, user impact, or strategic outcome).",
        "minutes": 3,
    },
    "has_scope_in": {
        "field": "scope_in",
        "message": "List in-scope items (modules, files, behaviors) so the developer knows where to act.",
        "minutes": 5,
    },
    "has_acceptance_criteria": {
        "field": "acceptance_criteria",
        "message": "Define at least one Given/When/Then acceptance criterion with a verifiable_by method.",
        "minutes": 10,
    },
    "has_validation_commands": {
        "field": "validation_commands",
        "message": "Add commands that verify the change (e.g. 'uv run pytest -q', 'ruff check').",
        "minutes": 3,
    },
    "has_size": {
        "field": "size",
        "message": "Pick a T-shirt size (XS/S/M/L/XL) so we can plan WIP capacity.",
        "minutes": 1,
    },
    "has_wip_tag": {
        "field": "wip_tag",
        "message": "Set a wip_tag (feature_work / bugfix / tech_debt / support) for capacity tracking.",
        "minutes": 1,
    },
}

# Sort order for rendering — blocking first, low last.
SEVERITY_ORDER: dict[RecommendationSeverity, int] = {
    "blocking": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


def _recommendation_for(
    check: DoRCheckItem,
    *,
    is_required: bool,
    config: ReadinessConfig,
) -> Recommendation | None:
    """Build a recommendation for one failed DoR check."""
    template = CHECK_RECOMMENDATIONS.get(check.key)
    if template is None:
        return None
    severity: RecommendationSeverity = "blocking" if is_required else "low"
    delta = config.penalty_required if is_required else config.penalty_optional
    return Recommendation(
        field=template["field"],
        severity=severity,
        message=template["message"],
        expected_score_delta=delta,
        estimated_minutes=template["minutes"],
    )


def build_recommendations(
    dor: DoREvaluation,
    *,
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> list[Recommendation]:
    """Build a sorted recommendation list from a DoR evaluation.

    - Failed REQUIRED checks → severity='blocking', delta = penalty_required.
    - Failed OPTIONAL checks → severity='low', delta = penalty_optional.
    - Passed checks → no recommendation (nothing to suggest).
    - Unknown check keys (shouldn't happen) → silently skipped.

    Sort: blocking → high → medium → low (within a severity, original
    DOR_CHECK_KEYS order is preserved for stable rendering).
    """
    recs: list[Recommendation] = []
    for check in dor.checks:
        if check.passed:
            continue
        rec = _recommendation_for(
            check, is_required=check.key in dor.required, config=config
        )
        if rec is not None:
            recs.append(rec)
    recs.sort(key=lambda r: SEVERITY_ORDER[r.severity])
    return recs


async def build_for_task(
    db,
    task_id: int,
    *,
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> list[Recommendation]:
    """Async wrapper: load the task, evaluate DoR, build recommendations."""
    dor = await evaluate_dor(db, task_id)
    return build_recommendations(dor, config=config)


async def calculate_readiness_with_recommendations(
    db,
    task_id: int,
    *,
    explain: bool = False,
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> ReadinessReport:
    """End-to-end: ReadinessReport with score, dor checks, risks, and
    populated recommendations — single DB roundtrip per data source.

    Lives here (not in readiness.py) to keep readiness free of any
    knowledge of the recommendation engine. The dependency direction
    stays one-way: recommendations -> readiness/dor.
    """
    dor = await evaluate_dor(db, task_id)
    row = await repo.get_task(db, task_id)
    risks_raw = row["risks"] if row is not None and "risks" in row.keys() else None
    risks = parse_risks_from_row(risks_raw)

    score, components = calculate_score_from_data(dor=dor, risks=risks, config=config)
    recs = build_recommendations(dor, config=config)

    return ReadinessReport(
        score=score,
        dor_passed=dor.passed,
        dor_checks=dor.checks,
        risks=risks,
        recommendations=recs,
        explain=[c.to_dict() for c in components] if explain else None,
    )


__all__ = [
    "CHECK_RECOMMENDATIONS",
    "SEVERITY_ORDER",
    "build_for_task",
    "build_recommendations",
    "calculate_readiness_with_recommendations",
]
