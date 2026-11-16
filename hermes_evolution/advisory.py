"""Bounded plans for Hermes research, delegation and operational diagnosis."""

from __future__ import annotations

import json

KINDS = ("research", "delegation", "operations")
EFFORTS = ("quick", "standard", "deep")

TOOL = {
    "name": "hermes_evolution_plan",
    "description": "Get a read-only plan for bounded research, Hermes delegation or operational diagnosis.",
    "parameters": {
        "type": "object",
        "properties": {"kind": {"type": "string", "enum": list(KINDS)},
                       "effort": {"type": "string", "enum": list(EFFORTS)},
                       "durable": {"type": "boolean", "description": "The handoff must survive a restart"}},
        "required": ["kind"],
        "additionalProperties": False,
    },
}


def plan(kind: str, effort: str = "standard", durable: bool = False) -> dict:
    if kind not in KINDS or effort not in EFFORTS or type(durable) is not bool:
        raise ValueError("Expected a supported plan kind, effort and optional boolean durable flag")
    queries, extracts, parallel = dict(quick=(2, 2, 1), standard=(4, 4, 2), deep=(8, 6, 3))[effort]
    if kind == "research":
        return {
            "kind": kind, "suggested_budget": {"search_queries": queries, "page_extracts": extracts,
                                                "parallel_lanes": parallel},
            "native_surface": "kanban" if durable else "delegate_task" if parallel > 1 else "current_agent",
            "steps": ["Write the questions and evidence needed to answer them.",
                      "Use web_search for independent angles. Limit extracts to primary sources and pages needed as evidence.",
                      "Verify authority, publication date and material claims. Record conflicts and missing evidence."],
            "parallel_roles": ["source_discovery", "evidence_verification", "contradiction_check"][:parallel],
            "deliverable": {"findings": "Claim with primary URL and relevant date", "open_questions": "What remains uncertain"},
            "stop_when": "Each material claim has sufficient evidence, or the remaining gap and next query are explicit.",
        }
    if kind == "delegation":
        return {
            "kind": kind, "native_surface": "kanban" if durable else "delegate_task",
            "suggested_budget": {"parallel_lanes": parallel, "max_review_passes": 1 if effort == "quick" else 2},
            "steps": ["Split work only where tasks are independent and can be verified separately.",
                      "Give each worker a goal, input scope, permissions, deliverable and acceptance checks.",
                      "Bring back evidence and blockers. Review outcomes before opening or retrying another task."],
            "handoff_fields": ["summary", "evidence", "verification", "blockers", "residual_risk"],
            "stop_when": "The parent can use independently verified results or a named blocker is outstanding.",
        }
    return {
        "kind": kind, "suggested_budget": {"comparable_samples": 2 if effort == "quick" else 3},
        "steps": ["Inspect Hermes health, workload backlog and Linux pressure at the same time.",
                  "Separate current pressure from accumulated counters and identify the affected workload.",
                  "Choose one reversible intervention and measure the same signals again. Stop or revert on deterioration."],
        "deliverable": {"signal": "Before and after", "hypothesis": "Evidence and alternatives", "rollback": "Restoration check"},
    }


def tool_handler(args: dict, **_context) -> str:
    try:
        if not isinstance(args, dict) or set(args) - {"kind", "effort", "durable"}:
            raise ValueError("Unexpected planning field")
        result = plan(args.get("kind"), args.get("effort", "standard"), args.get("durable", False))
    except ValueError as error:
        result = {"error": str(error)}
    return json.dumps(result, ensure_ascii=True)
