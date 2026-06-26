"""``report_progress`` handler.

Builds the timeline event payload and emits it to AIStackWorks. The event has two
possible transports, tried in this order:

1. **MCP (preferred):** when ``MC_MCP_URL`` and ``MC_MCP_TOKEN`` are both set in
   the profile env, the event is POSTed as a JSON-RPC ``tools/call`` of the
   ``report_progress`` tool to AIStackWorks' stateless streamable-HTTP MCP server.
   This is a direct, authenticated Hermes→AIStackWorks call.
2. **Agent-host daemon UDS (fallback):** the event is POSTed to the local
   agent-host daemon over its host Unix socket (mounted into the container at
   ``/run/aistackworks``); the daemon relays it to AIStackWorks over its existing
   outbound WebSocket. This path needs no token or network egress from the agent
   and is the fallback whenever the MCP leg is absent or fails (so an older
   AIStackWorks deployment without the MCP tool, or a transient MCP error, never
   loses an event relative to the UDS-only behaviour).

Produced *assets* (demo recordings) always upload over the daemon UDS
(``/v1/agent-media``) regardless of which transport carries the event — media is
binary and does not belong on the JSON MCP leg.

Transport uses the stdlib only (``urllib.request`` for MCP, a raw HTTP/1.1
request over an ``AF_UNIX`` socket for the daemon), so it has no dependency on the
gateway venv's package set and cannot be broken by an upstream image change.
Best-effort by contract: any failure returns a JSON error string but never raises
into the agent.
"""
from __future__ import annotations

import http.client
import json
import logging
import mimetypes
import os
import re
import socket
import threading
import urllib.error
import urllib.request

from .identity import current

_log = logging.getLogger("hermes_plugins.aistackworks_timeline")

# Matches the daemon's --agent-event-sock default and the AGENT_EVENT_SOCK the
# launcher already exports. Env override kept for parity with the daemon flag.
_SOCK = os.environ.get("AGENT_EVENT_SOCK", "/run/aistackworks/agent.sock")
_TIMEOUT_S = 5.0
# Demo recordings are larger and the relay hop to AIStackWorks is slower, so give the
# media upload its own (longer) timeout.
_MEDIA_TIMEOUT_S = 60.0
# The MCP tools/call leg has its own (network) timeout.
_MCP_TIMEOUT_S = 10.0


def _mcp_env() -> "tuple[str, str] | None":
    """The ``(url, token)`` for the AIStackWorks MCP server, or ``None``.

    Read fresh on every emit (not module-import) so a profile that exports
    ``MC_MCP_URL`` / ``MC_MCP_TOKEN`` after this module loads still takes the MCP
    path — these are the same vars the profile's ``config.yaml`` interpolates for
    its MCP server registration. Both must be set for the MCP leg to be tried.
    """
    url = (os.environ.get("MC_MCP_URL") or "").strip()
    token = (os.environ.get("MC_MCP_TOKEN") or "").strip()
    if url and token:
        return url, token
    return None


def _emit_via_mcp(url: str, token: str, payload: dict) -> bool:
    """POST the event as a JSON-RPC ``tools/call`` to the AIStackWorks MCP server.

    Sends ``report_progress`` with the MC tool's exact argument names. The server
    synthesizes its own canonical ``event_key`` (``card-<id>:<skill>:<iter>:
    <status>``), so ``event_key`` is intentionally NOT sent — only the UDS leg
    carries it. Returns ``True`` only on a clean JSON-RPC result; any connection
    error, non-2xx, malformed body, or JSON-RPC ``error`` object (incl. an older
    deployment that lacks the tool) returns ``False`` so the caller falls back to
    the daemon UDS path.
    """
    arguments = {
        "card_id": payload["card_id"],
        "skill": payload["skill"],
        "status": payload["status"],
        "headline": payload["headline"],
        # Always sent explicitly (report_progress normalized it, defaulting to 1
        # like the MC tool) so the server-synthesized event_key is byte-identical
        # to the locally computed one the UDS fallback would carry.
        "iter": payload["iter"],
        "fields": payload.get("fields") or {},
        "sections": payload.get("sections") or [],
        "artifact": payload.get("artifact"),
        "session_id": payload.get("session_id"),
    }

    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "report_progress", "arguments": arguments},
    }
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_MCP_TIMEOUT_S) as resp:
            if not 200 <= resp.status < 300:
                return False
            raw = resp.read()
    except (urllib.error.URLError, OSError, ValueError):
        return False

    # Stateless streamable-HTTP returns one JSON response per POST
    # (json_response=True). Parse it and reject a JSON-RPC error object — a
    # "method/tool not found" on an older AIStackWorks must fall back to UDS.
    try:
        envelope_out = json.loads(raw or b"{}")
    except ValueError:
        return False
    if not isinstance(envelope_out, dict) or envelope_out.get("error"):
        return False
    result = envelope_out.get("result")
    if not isinstance(result, dict):
        return False
    # A tool that ran but reported a tool-level error (isError) is not a success;
    # fall back so the UDS leg still records the event.
    if result.get("isError"):
        return False
    return True


def _sanitize_field_value(value):
    """Coerce a field value to the shapes AIStackWorks renders cleanly: a scalar,
    or a list of scalars. Models occasionally emit a nested object where a
    string belongs (e.g. ``["7/7 pass", {"Commit count": "7"}]``) — flatten it
    to ``"key: value"`` text so the timeline never shows raw JSON."""
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    if isinstance(value, list):
        return [
            item if isinstance(item, (str, int, float, bool)) else _sanitize_field_value(item)
            for item in value
        ]
    return value


def _sanitize_fields(fields):
    if not isinstance(fields, dict):
        return {}
    return {k: _sanitize_field_value(v) for k, v in fields.items()}


_TRANSCRIPT_MAX_BYTES = int(os.environ.get("AISTACKWORKS_TRANSCRIPT_MAX_BYTES", str(5 * 1024 * 1024)))
# Terminal stage-result statuses whose rendered task log is worth capturing. This
# must cover BOTH dialects MC's EVENT_STATUS_MAP accepts: the "stage handoff"
# keywords the live skills actually emit (build→ready_for_test, test→awaiting_demo,
# demo→awaiting_ship, refine→awaiting_prd_review, ship→shipped/ready_to_merge) AND
# the "lifecycle"/backstop vocabulary (SKILL_SUCCESS_STATUS: …→done, review→passed).
# It also captures terminal failures (review→failed, blocked) — the blocked/failed
# transcript is the most valuable one to inspect. ``started`` is intentionally
# excluded so stage-pickup rows never upload a partial log.
_TRANSCRIPT_TERMINAL_STATUSES = {
    # stage-handoff dialect
    "ready_for_test",
    "awaiting_demo",
    "awaiting_ship",
    "awaiting_prd_review",
    # lifecycle / backstop dialect
    "done",
    "pass",
    "passed",
    # terminal results + ship / merge-queue
    "failed",
    "blocked",
    "ready_to_merge",
    "shipped",
    "merged",
    "released",
}


class _UDSConnection(http.client.HTTPConnection):
    """``http.client`` over an ``AF_UNIX`` socket — gives us full, robust
    response parsing (status + body) for the media-upload round-trip, which the
    status-line-only ``_post_uds`` can't do."""

    def __init__(self, sock_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._sock_path)
        self.sock = sock


def _upload_bytes(sock_path: str, *, card_id: str, event_key: str, filename: str,
                  content_type: str, data: bytes, media_kind: str,
                  timeout: float) -> str | None:
    """Upload bytes to AIStackWorks via the daemon UDS; return the hosted href.

    POSTs the raw bytes to ``/v1/agent-media`` with card/event/filename/type
    headers (the daemon wraps it as multipart for AIStackWorks and adds its
    bearer). AIStackWorks returns ``url`` for videos and ``href`` for
    attachments; accept either. Best-effort by contract.
    """
    conn = _UDSConnection(sock_path, timeout)
    try:
        conn.request(
            "POST", "/v1/agent-media", body=data,
            headers={
                "Host": "localhost",
                "Content-Type": content_type,
                "Content-Length": str(len(data)),
                "X-Card-Id": card_id,
                "X-Event-Key": event_key,
                "X-Filename": filename,
                "X-Media-Kind": media_kind,
            },
        )
        resp = conn.getresponse()
        raw = resp.read()
        if not 200 <= resp.status < 300:
            _log.warning(
                "demo asset upload failed: daemon %s POST /v1/agent-media -> HTTP %s %s "
                "(is the daemon binary current? the route 404s on builds before the "
                "/v1/agent-media commit)",
                sock_path, resp.status, (raw[:200].decode("utf-8", "replace") if raw else ""),
            )
            return None
        payload = json.loads(raw or b"{}") or {}
        href = payload.get("url") or payload.get("href")
        _log.info("%s asset uploaded -> %s", media_kind, href)
        return href
    except (OSError, ValueError) as exc:
        _log.warning("%s asset upload errored talking to daemon %s: %s", media_kind, sock_path, exc)
        return None
    finally:
        conn.close()


def _upload_asset(sock_path: str, card_id: str, event_key: str, asset_path: str,
                  timeout: float) -> str | None:
    """Upload ``asset_path`` to AIStackWorks via the daemon UDS; return the hosted URL."""
    try:
        with open(asset_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None

    filename = os.path.basename(asset_path) or "asset"
    content_type = mimetypes.guess_type(asset_path)[0] or "application/octet-stream"
    return _upload_bytes(
        sock_path, card_id=card_id, event_key=event_key, filename=filename,
        content_type=content_type, data=data, media_kind="demo", timeout=timeout,
    )


def upload_transcript(card_id: str, event_key: str, transcript: str, *, filename: str = "transcript.log") -> str | None:
    """Best-effort upload of a text transcript attachment for one timeline event."""
    data = transcript.encode("utf-8", errors="replace")
    safe_name = os.path.basename(filename) or "transcript.log"
    return _upload_bytes(
        _SOCK, card_id=card_id, event_key=event_key, filename=safe_name,
        content_type="text/plain; charset=utf-8", data=data,
        media_kind="transcript", timeout=_MEDIA_TIMEOUT_S,
    )


def _normalize_event_key(event_key: str, status: str | None = None) -> str:
    """Mirror Mission Control's storage key normalization for transcript links."""
    normalized_status = (status or "").strip().lower()
    if normalized_status == "blocked" or event_key.endswith(":blocked"):
        return re.sub(r":\d+:blocked$", "::blocked", event_key)
    return event_key


def _is_transcript_terminal_status(status: str) -> bool:
    normalized = (status or "").strip().lower()
    return normalized in _TRANSCRIPT_TERMINAL_STATUSES or normalized.startswith("blocked")


def _current_task_log_path() -> str:
    """Discover ``~/.hermes/kanban/logs/t_<taskid>.log`` for this worker."""
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if not task_id:
        return ""
    candidates: list[str] = []
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        candidates.append(os.path.join(hermes_home, "kanban", "logs", f"{task_id}.log"))
    candidates.append(os.path.expanduser(os.path.join("~", ".hermes", "kanban", "logs", f"{task_id}.log")))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def _read_current_task_log() -> tuple[str, bytes] | None:
    path = _current_task_log_path()
    if not path:
        return None
    try:
        size = os.path.getsize(path)
        if size <= 0 or size > _TRANSCRIPT_MAX_BYTES:
            return None
        with open(path, "rb") as fh:
            return path, fh.read()
    except OSError:
        return None


def _upload_current_task_log(card_id: str, event_key: str, status: str) -> str | None:
    """Upload the active worker's terminal transcript log for terminal events.

    Best-effort: missing, empty, or oversized logs are silently skipped so
    timeline emission is never blocked by transcript capture.
    """
    if not _is_transcript_terminal_status(status):
        return None
    found = _read_current_task_log()
    if not found:
        return None
    path, data = found
    return _upload_bytes(
        _SOCK,
        card_id=card_id,
        event_key=_normalize_event_key(event_key, status),
        filename=os.path.basename(path) or "transcript.log",
        content_type="text/plain; charset=utf-8",
        data=data,
        media_kind="transcript",
        timeout=_MEDIA_TIMEOUT_S,
    )

# Track which (card_id, skill) milestones the current session has already
# emitted, so the Kanban-transition backstop hook only fires when the skill
# itself didn't. Reset per session by the on_session_start hook.
_emitted_lock = threading.Lock()
_emitted: set[tuple[str, str]] = set()
_emitted_event_keys: dict[tuple[str, str], str] = {}


def mark_emitted(card_id: str, skill: str, event_key: str = "") -> None:
    with _emitted_lock:
        key = (card_id, skill)
        _emitted.add(key)
        if event_key:
            _emitted_event_keys[key] = event_key


def already_emitted(card_id: str, skill: str) -> bool:
    with _emitted_lock:
        return (card_id, skill) in _emitted


def emitted_event_key(card_id: str, skill: str) -> str:
    with _emitted_lock:
        return _emitted_event_keys.get((card_id, skill), "")


def reset_emitted() -> None:
    with _emitted_lock:
        _emitted.clear()
        _emitted_event_keys.clear()


def _emit_event(payload: dict) -> dict:
    """Emit a built timeline event, MCP-first with a UDS fallback.

    Tries the AIStackWorks MCP ``report_progress`` tools/call when ``MC_MCP_URL`` +
    ``MC_MCP_TOKEN`` are set; on ANY MCP failure (or when the env is absent) falls
    back to the daemon UDS ``/v1/agent-event`` POST, so no event is ever lost
    relative to the UDS-only behaviour. Returns the model-facing result dict
    (``ok``, ``transport``, plus per-transport detail). Best-effort: never raises.
    """
    mcp = _mcp_env()
    if mcp is not None:
        url, token = mcp
        if _emit_via_mcp(url, token, payload):
            return {"ok": True, "transport": "mcp", "event_key": payload["event_key"]}
        # MCP failed (unreachable, non-2xx, JSON-RPC error, or tool absent on an
        # older AIStackWorks) — fall through to the daemon UDS leg.

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    try:
        code = _post_uds(_SOCK, "/v1/agent-event", body, _TIMEOUT_S)
    except OSError as exc:
        return {"ok": False, "transport": "uds", "reason": f"daemon unreachable: {exc}"}
    ok = 200 <= code < 300
    return {"ok": ok, "transport": "uds", "status": code, "event_key": payload["event_key"]}


def _post_uds(sock_path: str, path: str, body: bytes, timeout: float) -> int:
    """Minimal HTTP/1.1 POST over a Unix socket. Returns the status code."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(sock_path)
        request = (
            f"POST {path} HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii") + body
        conn.sendall(request)
        # Read just enough to parse the status line; body is irrelevant.
        head = conn.recv(64)
        # b"HTTP/1.1 202 Accepted..." -> 202
        parts = head.split(b" ", 2)
        return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    finally:
        conn.close()


def report_progress(args: dict, **_kwargs) -> str:
    # Identity precedence: an explicit ``card_id`` arg wins over the
    # session-derived one. ``main`` is dispatched *as* session ``card-<id>`` so
    # ``current()`` is correct for it, but the coder/reviewer/ship steps run as
    # separate Kanban tasks whose session id is the task id — NOT
    # ``card-<notion_page_id>``. Those skills read the card_id from their task
    # body and pass it here so the event resolves to the right card in AIStackWorks.
    ident_card, ident_session = current()
    card_id = (args.get("card_id") or "").strip() or ident_card
    if not card_id:
        # No dispatch identity captured (e.g. invoked outside an AIStackWorks dispatch).
        return json.dumps({"ok": False, "reason": "no card identity for this session"})
    # ``session_id`` is stored/displayed by AIStackWorks; keep the session-derived id when
    # present (the task id for workers) for traceability. The ``event_key`` AIStackWorks
    # dedups on, however, is **canonical card-based** so a skill's live call and
    # the worker-exit backstop produce the SAME key for a given
    # (card, skill, iter, status) and never double-post.
    session_id = ident_session or f"card-{card_id}"
    event_session = f"card-{card_id}"

    skill = args.get("skill")
    status = args.get("status")
    headline = args.get("headline")
    if not skill or not status or not headline:
        return json.dumps({"ok": False, "reason": "skill, status and headline are required"})

    # Normalize ``iter`` exactly as the MC MCP tool does (missing/invalid → 1) so
    # BOTH transports always compute the IDENTICAL canonical event_key for
    # identical inputs. This matters for dedup across legs: if an MCP POST
    # persists server-side but its response is lost (timeout/reset after write),
    # the UDS fallback re-emits the event — the keys MUST match or AIStackWorks
    # records a duplicate TimelineEvent.
    try:
        it = int(args["iter"]) if args.get("iter") is not None else 1
    except (TypeError, ValueError):
        it = 1
    # Canonical card-based event_key so a skill's live call and the worker-exit
    # backstop produce the SAME key for one (card, skill, iter, status) and AIStackWorks
    # dedups them (it is also the asset upload's X-Event-Key below).
    event_key = f"{event_session}:{skill}:{it}:{status}"

    artifact = args.get("artifact")
    # Produced asset (e.g. a /demo recording): upload it to AIStackWorks via the daemon and
    # point the timeline artifact at the hosted URL (videos play inline). Keep
    # the caller's artifact kind/label; only fill the href. Best-effort — a
    # failed upload leaves the passed artifact untouched and never blocks the event.
    asset = args.get("asset")
    if asset:
        url = _upload_asset(_SOCK, card_id, event_key, asset, _MEDIA_TIMEOUT_S)
        if url:
            base = artifact or {}
            artifact = {
                "kind": base.get("kind") or "demo",
                "label": base.get("label") or os.path.basename(asset),
                "href": url,
            }

    payload = {
        "card_id": card_id,
        "session_id": session_id,
        "skill": skill,
        "status": status,
        "headline": headline,
        "iter": it,
        "fields": _sanitize_fields(args.get("fields") or {}),
        "sections": args.get("sections") or [],
        "artifact": artifact,
        # event_key is carried by the UDS leg only; the MCP server synthesizes its
        # own (same formula), so _emit_via_mcp drops it from the tools/call args.
        "event_key": event_key,
    }

    result = _emit_event(payload)
    if result.get("ok"):
        mark_emitted(card_id, skill)
        _upload_current_task_log(card_id, payload["event_key"], status)
    return json.dumps(result)
