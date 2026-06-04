"""Capture the dispatch's card/session identity in-process, per the gateway run.

Why this exists
---------------
AIStackWorks dispatches a card as session id ``card-<notion_page_id>``. On the
agent-host relay path the daemon now sends that value as the native
``X-Hermes-Session-Id`` header (DISP-04), so Hermes adopts it as its own session
id and hands it to plugin hooks. The Hermes plugin runtime passes ``session_id``
to *hook* callbacks (``on_session_start`` / ``pre_llm_call``) but **not** to tool
handlers — so we stash the current card here from the hooks and let the
``report_progress`` tool read it back.

Worker sessions
---------------
``main`` is dispatched *as* session ``card-<id>``, so deriving the card from the
session id is correct for it. But the coder/reviewer/ship steps run as separate
Kanban tasks whose session id is the **task id**, not ``card-<id>`` — deriving
from the session there yields the wrong card. For those we read the real card id
out of the task body (``main``/refine writes ``card_id: <id>`` as its first
line), looked up from the Kanban sqlite DB the dispatcher pins via
``HERMES_KANBAN_DB`` + ``HERMES_KANBAN_TASK``. This makes ``report_progress``
resolve the right card in *every* session — the explicit ``card_id`` tool arg is
then a belt-and-suspenders override, not the only path.

Concurrency
-----------
Hermes containers are single-agent-per-profile and handle one dispatch at a time,
so a single "current identity" is correct. ``pre_llm_call`` refreshes it every
turn, which also covers resumed sessions where ``on_session_start`` may not fire.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading

_lock = threading.Lock()
_current: dict[str, str] = {"session_id": "", "card_id": ""}

# ``card_id: <id>`` on its own line, tolerating ``*card_id: `<id>`*`` styling.
_CARD_ID_RE = re.compile(r"(?mi)^\s*\*?\s*card_id\s*[:=]\s*[`\"']?([\w.-]+)")


def _derive_card_id(session_id: str) -> str:
    s = (session_id or "").strip()
    for prefix in ("plan-card-", "card-"):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def _kanban_db_path() -> str:
    """Resolve the Kanban sqlite path the dispatcher pins for workers."""
    for env in ("HERMES_KANBAN_DB",):
        p = os.environ.get(env, "").strip()
        if p and os.path.isfile(p):
            return p
    # Fallbacks: the umbrella home or the conventional /opt/data location.
    for base in (os.environ.get("HERMES_KANBAN_HOME", ""), os.environ.get("HERMES_HOME", ""), "/opt/data"):
        if base:
            cand = os.path.join(base.strip(), "kanban.db")
            if os.path.isfile(cand):
                return cand
    return ""


def read_task(task_id: str) -> dict:
    """Best-effort read of a Kanban task row → ``{card_id, skills, title, status}``.

    Pure stdlib (read-only sqlite) so the plugin stays decoupled from Hermes.
    Returns ``{}`` on any error (no DB, missing row, etc.).
    """
    task_id = (task_id or "").strip()
    db_path = _kanban_db_path()
    if not task_id or not db_path:
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT body, skills, title, status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return {}
    if not row:
        return {}
    body, skills_raw, title, status = row
    card_id = ""
    if body:
        m = _CARD_ID_RE.search(body)
        if m:
            card_id = m.group(1).strip()
    skills: list[str] = []
    if skills_raw:
        try:
            import json
            parsed = json.loads(skills_raw)
            if isinstance(parsed, list):
                skills = [str(s) for s in parsed]
        except Exception:
            skills = []
    return {"card_id": card_id, "skills": skills, "title": title or "", "status": status or ""}


def read_latest_run(task_id: str) -> dict:
    """Best-effort read of a task's most recent ``task_runs`` row.

    Returns ``{summary, metadata, outcome, status}`` (``metadata`` parsed from
    JSON to a dict when possible, else ``{}``). The worker-exit backstop reads
    this to build a RICH timeline event — the coder/reviewer/ship skills already
    write structured ``summary`` + ``metadata`` to ``kanban_complete`` (PR url,
    branch, changed_files, commits, QA findings, MERGE_SHA), so the backstop can
    surface real per-stage detail without depending on the model calling a tool.

    Pure stdlib (read-only sqlite). Returns ``{}`` on any error.
    """
    task_id = (task_id or "").strip()
    db_path = _kanban_db_path()
    if not task_id or not db_path:
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT summary, metadata, outcome, status FROM task_runs "
                "WHERE task_id = ? ORDER BY COALESCE(ended_at, started_at) DESC, id DESC "
                "LIMIT 1",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return {}
    if not row:
        return {}
    summary, metadata_raw, outcome, status = row
    metadata: dict = {}
    if metadata_raw:
        try:
            import json
            parsed = json.loads(metadata_raw)
            if isinstance(parsed, dict):
                metadata = parsed
        except Exception:
            metadata = {}
    return {
        "summary": summary or "",
        "metadata": metadata,
        "outcome": outcome or "",
        "status": status or "",
    }


def _card_id_from_kanban_task() -> str:
    """The active worker's card id from its Kanban task body (or "")."""
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if not task_id:
        return ""
    return read_task(task_id).get("card_id", "")


def remember(session_id: str) -> None:
    """Record the active dispatch's session id (and resolved card id).

    Card id precedence: the worker's Kanban task body (``card_id:``) wins — it is
    authoritative for coder/reviewer/ship — then the session-derived id (correct
    for ``main``, dispatched as ``card-<id>``).
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return
    card_id = _card_id_from_kanban_task() or _derive_card_id(session_id)
    with _lock:
        _current["session_id"] = session_id
        _current["card_id"] = card_id


def current() -> tuple[str, str]:
    """Return ``(card_id, session_id)`` for the active dispatch (may be empty)."""
    with _lock:
        return _current["card_id"], _current["session_id"]
