"""aistackworks-timeline Hermes plugin.

Captures the dispatched card identity from session hooks and exposes a
``report_progress`` tool that skills call at milestones to populate the card's
AIStackWorks timeline.

Two layers drive the card's timeline so AIStackWorks never goes blind on a stage:

1. **Skill-driven (primary, granular):** build/test/demo/ship/refine call
   ``report_progress`` at each milestone — rich ``fields``/``sections``.
2. **Worker-exit backstop (deterministic):** dispatcher-spawned workers complete
   by *returning a summary* (the dispatcher auto-completes the task) — they do NOT
   reliably call ``kanban_complete``. So the backstop fires on **session end**
   (``on_session_finalize`` / ``on_session_end``), the real "worker finished"
   signal, and emits **every** mapped stage of the task (a multi-stage task →
   each of build/test/demo with status ``done``; a ship task → ``ship/done``)
   that the skill didn't report itself, from the task's run record. The
   completion-status vocabulary is the kanban-execution one (build/test/demo/docs
   → ``done``, review → ``passed``, ship → ``done``); ``blocked`` is emitted for a
   worker that died or blocked. The tool and the backstop run in the SAME process,
   so ``tools.already_emitted`` is exact — the backstop fills only the genuine
   gaps (no double-post). Skipped if the task ended ``blocked``; a
   ``post_tool_call`` hook still catches an explicit ``kanban_block``.
"""
from __future__ import annotations

import logging
import os

from . import identity, schemas, tools

logger = logging.getLogger(__name__)

# Terminal *completion* status AIStackWorks advances off, per stage skill — the
# authoritative kanban-execution vocabulary (kanban-execution-contracts.md §2):
# build/test/demo/docs complete with "done", review completes with "passed" (its
# failure is "passed"→"failed", which the skill emits live — not a backstop
# success), ship completes with "done". A worker that simply finished its task
# emits its stage's completion status here; one that died/blocked emits "blocked"
# (handled separately below, never as a success).
SKILL_SUCCESS_STATUS: dict[str, str] = {
    "build": "done",
    "test": "done",
    "demo": "done",
    "review": "passed",
    "docs": "done",
    "ship": "done",
}

# Clean framing TITLE for the worker-exit (auto) event per terminal skill. The
# verbose run summary goes in the body section; the headline reads as a title.
_BACKSTOP_TITLE: dict[str, str] = {
    "build": "What was built",
    "test": "QA results",
    "demo": "What was done",
    "review": "Review outcome",
    "docs": "What was documented",
    "ship": "What shipped",
}
def _on_session_start(session_id="", **_kwargs) -> None:
    identity.remember(session_id)
    tools.reset_emitted()


def _on_pre_llm_call(session_id="", **_kwargs) -> None:
    # Refresh every turn — covers resumed sessions where on_session_start may
    # not fire again.
    identity.remember(session_id)


def _unwrap(value):
    """Unwrap Codex's ``{"item": [...]}`` list serialization to the bare value."""
    if isinstance(value, dict) and set(value.keys()) == {"item"}:
        return value["item"]
    return value


def _mget(metadata: dict, *keys: str):
    """Case-insensitive metadata lookup (the model varies key casing:
    ``pr_url`` vs ``PR_URL``), with the Codex ``{"item": ...}`` wrapper unwrapped."""
    low = {str(k).lower(): v for k, v in (metadata or {}).items()}
    for key in keys:
        if key.lower() in low:
            return _unwrap(low[key.lower()])
    return None


def _as_chip(value) -> str:
    """Render a metadata value as a short field chip string."""
    value = _unwrap(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(str(_unwrap(v)) for v in value)
    return str(value)


def _rich_event(card_id: str, skill: str, status: str, run: dict) -> dict:
    """Build a RICH ``report_progress`` payload from a finished task's run record.

    Pulls the structured detail the stage skills already write to
    ``kanban_complete`` (PR url, branch, changed files, commits, QA findings,
    MERGE_SHA) so the deterministic worker-exit event carries real per-stage
    detail — not just ``"<skill> <status> (auto)"``.
    """
    summary = (run.get("summary") or "").strip()
    meta = run.get("metadata") or {}

    # Headline is a clean framing TITLE (the worker-completion event is shown to
    # a human as the session record), with the verbose run summary moved to the
    # body section below — not the summary's first line dumped as the title.
    headline = _BACKSTOP_TITLE.get(skill, "What was done")

    fields: dict = {}
    pr_url = _mget(meta, "pr_url")
    branch = _mget(meta, "branch")
    commits = _mget(meta, "commits")
    changed = _mget(meta, "changed_files", "files", "files_touched")
    merge_sha = _mget(meta, "merge_sha")
    base_branch = _mget(meta, "base_branch")
    tests_run = _mget(meta, "tests_run")
    if pr_url:
        fields["PR_URL"] = _as_chip(pr_url)
    if branch:
        fields["BRANCH"] = _as_chip(branch)
    if changed is not None:
        fields["FILES"] = _as_chip(changed)
    if commits is not None:
        fields["COMMITS"] = _as_chip(commits)
    if merge_sha:
        fields["MERGE_SHA"] = _as_chip(merge_sha)
    if base_branch:
        fields["BASE_BRANCH"] = _as_chip(base_branch)
    if tests_run is not None:
        fields["TESTS_RUN"] = _as_chip(tests_run)

    sections: list[dict] = []
    if summary:
        sections.append({"heading": "Summary", "body": summary})
    sections.append({"heading": "Source", "body":
                     "Auto-emitted on worker completion from the task's run "
                     "record (the skill did not emit this milestone itself)."})

    artifact = None
    if pr_url:
        artifact = {"kind": "pr", "label": "PR", "href": _as_chip(pr_url)}
    elif merge_sha:
        artifact = {"kind": "commit", "label": str(_as_chip(merge_sha))}

    payload: dict = {
        "card_id": card_id, "skill": skill, "status": status,
        "headline": headline, "fields": fields, "sections": sections,
    }
    if artifact:
        payload["artifact"] = artifact
    return payload


def _task_stage_skills(meta: dict) -> list[str]:
    """Every AIStackWorks-advancing stage skill this finished task covers, in stage order.

    A multi-stage task carries an explicit ``skills`` list (e.g.
    ``[build, test, demo]``) → those, in order. A single-stage task (the
    kanban-execution model seeds one task per stage) has no skills list but a
    stage-prefixed title (``build:``/``test:``/``demo:``/``review:``/``docs:``/
    ``ship:``) → that one stage."""
    skills = [s for s in (meta.get("skills") or []) if s in SKILL_SUCCESS_STATUS]
    if skills:
        return skills
    # No skills list — infer the single stage from the title prefix.
    title = (meta.get("title") or "").strip().lower()
    for stage in SKILL_SUCCESS_STATUS:  # build/test/demo/review/docs/ship
        if title.startswith(f"{stage}:") or f"/{stage}" in title:
            return [stage]
    return []  # unknown → not an AIStackWorks-advancing stage


def _terminal_skill(meta: dict) -> str:
    """The single milestone skill a finishing task maps to (or ""). Used by the
    ``kanban_block`` hook to mark a stage blocked."""
    skills = _task_stage_skills(meta)
    return skills[-1] if skills else ""


def _worker_terminal_backstop() -> None:
    """Deterministically emit any of the worker task's stage milestones the skill
    didn't report itself.

    Resolves the task from ``$HERMES_KANBAN_TASK`` and emits EVERY mapped stage
    (coder → build/test/demo, ship → shipped) that isn't already in
    ``already_emitted``, in stage order, from the task's run record. The tool and
    the backstop share this process, so ``already_emitted`` is exact — the
    backstop fills only the genuine gaps (no double-post). Skipped entirely if the
    task ended ``blocked`` (handled by the ``kanban_block`` hook).
    """
    tid = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if not tid:
        return  # not a dispatcher-spawned worker (e.g. main's refine dispatch)
    meta = identity.read_task(tid)
    card_id = meta.get("card_id") or identity.current()[0]
    skills = _task_stage_skills(meta)
    if not card_id or not skills:
        return
    if (meta.get("status") or "").strip().lower() == "blocked":
        return  # a block is handled by the kanban_block hook, not as success
    run = identity.read_latest_run(tid)
    for skill in skills:  # stage order: build → test → demo (or just ship)
        status = SKILL_SUCCESS_STATUS.get(skill)
        if not status or tools.already_emitted(card_id, skill):
            continue  # the skill reported this stage live; don't duplicate
        tools.report_progress(_rich_event(card_id, skill, status, run))


def _on_session_finalize(session_id="", **_kwargs) -> None:
    try:
        _worker_terminal_backstop()
    except Exception:  # never let a backstop break the agent
        logger.debug("aistackworks-timeline session-finalize backstop failed", exc_info=True)


def _on_post_tool_call(tool_name="", args=None, result=None, task_id="", session_id="", **_kwargs) -> None:
    """Catch an explicit ``kanban_block`` → emit the task's stage as blocked."""
    if tool_name != "kanban_block":
        return
    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK", "")).strip()
    meta = identity.read_task(tid) if tid else {}
    card_id = meta.get("card_id") or identity.current()[0]
    skill = _terminal_skill(meta)
    if not card_id or not skill:
        return
    try:
        tools.report_progress({
            "card_id": card_id, "skill": skill, "status": "blocked",
            "headline": f"{skill} blocked (auto)",
            "sections": [{"heading": "Source", "body":
                          "Auto-emitted on kanban_block — the task was blocked."}],
        })
    except Exception:
        logger.debug("aistackworks-timeline block backstop failed", exc_info=True)


def register(ctx) -> None:
    ctx.register_tool(
        name="report_progress",
        toolset="aistackworks-timeline",
        schema=schemas.REPORT_PROGRESS,
        handler=tools.report_progress,
    )
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    # Worker-exit is the reliable completion signal (workers don't reliably call
    # kanban_complete — the dispatcher auto-completes on summary return).
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    ctx.register_hook("on_session_end", _on_session_finalize)
    logger.info(
        "aistackworks-timeline plugin registered "
        "(report_progress tool + session hooks + worker-exit backstop)"
    )
