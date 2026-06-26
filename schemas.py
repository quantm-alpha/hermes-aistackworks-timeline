"""Tool schema for ``report_progress`` — the LLM reads this description/params.

The catalog (which skill emits which milestone) lives in the skills themselves
(``agents/main/skills/*/SKILL.md``); this schema just defines the shape.
"""
from __future__ import annotations

REPORT_PROGRESS = {
    "name": "report_progress",
    "description": (
        "Post a milestone event to the current card's AIStackWorks timeline. "
        "Call this at the end of a skill stage (refine/build/test/demo/ship) to "
        "record durable progress for the human watching the card. Best-effort: a "
        "failure here never blocks the skill. Pass the `card_id` from your task "
        "body so the event resolves to the right card — required when you run as "
        "a spawned Kanban task (coder/reviewer/ship), whose session id is not the "
        "card's. (The `main` coordinator may omit it; its dispatch session "
        "already carries the card identity.)"
    ),
    "parameters": {
        "type": "object",
        "required": ["skill", "status", "headline"],
        "properties": {
            "card_id": {
                "type": "string",
                "description": (
                    "The card's id (the UUID the task was dispatched for). Pass it "
                    "verbatim so AIStackWorks can advance the right card."
                ),
            },
            "skill": {
                "type": "string",
                "enum": ["refine", "build", "test", "demo", "review", "ship", "docs"],
                "description": "The skill stage this milestone belongs to.",
            },
            "status": {
                "type": "string",
                "description": (
                    "Milestone status. This drives card workflow advancement in "
                    "AIStackWorks, so use the exact value your skill prescribes "
                    "(e.g. refine→'awaiting_prd_review', build→'ready_for_test', "
                    "test→'awaiting_demo', demo→'pass', ship→'shipped'; 'blocked' "
                    "for any blocker)."
                ),
            },
            "headline": {
                "type": "string",
                "description": "One-line summary, e.g. 'Build complete' or 'QA failed — 2/8 criteria'.",
            },
            "iter": {
                "type": "integer",
                "description": "Iteration/attempt number for repeatable stages (build/test). Omit for one-shot stages.",
            },
            "fields": {
                "type": "object",
                "description": (
                    "Flat label→value map shown as chips, e.g. "
                    "{'Files changed': '14 (+482/-91)', 'Branch': 'feat/foo'}. "
                    "Values MUST be scalars (string/number/boolean) or arrays of "
                    "scalars — never nested objects (they render as raw JSON)."
                ),
                "additionalProperties": {
                    "anyOf": [
                        {"type": ["string", "number", "boolean", "null"]},
                        {
                            "type": "array",
                            "items": {"type": ["string", "number", "boolean"]},
                        },
                    ]
                },
            },
            "sections": {
                "type": "array",
                "description": "Optional longer-form sections.",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string"},
                        "body": {
                            "type": "string",
                            "description": (
                                "Rendered verbatim with newlines preserved — "
                                "write multi-line text with '- ' bullets for "
                                "anything beyond a sentence or two, never one "
                                "run-on paragraph."
                            ),
                        },
                    },
                    "required": ["heading", "body"],
                },
            },
            "artifact": {
                "type": "object",
                "description": "Optional link to a produced artifact.",
                "properties": {
                    "kind": {"type": "string", "enum": ["commit", "pr", "demo"]},
                    "label": {"type": "string"},
                    "href": {"type": "string"},
                },
                "required": ["kind", "label"],
            },
            "asset": {
                "type": "string",
                "description": (
                    "Local path to a produced asset to host in AIStackWorks "
                    "(e.g. the /demo recording .mp4). When set, the plugin uploads "
                    "it via the agent-host daemon and points artifact.href at the "
                    "hosted AIStackWorks URL — videos then play inline on the timeline. "
                    "Best-effort: a failed upload leaves any artifact you passed "
                    "intact."
                ),
            },
        },
    },
}
