"""``report_progress`` handler.

Builds the ``timeline.append`` payload and POSTs it to the agent-host daemon over
its host Unix socket (mounted into the container at ``/run/aistackworks``). The
daemon relays the event to Mission Control over its existing outbound WebSocket —
there is no direct Hermes→MC network call.

Transport uses the stdlib only (a raw HTTP/1.1 request over an ``AF_UNIX``
socket), so it has no dependency on the gateway venv's package set and cannot be
broken by an upstream image change. Best-effort by contract: any failure returns
a JSON error string but never raises into the agent.
"""
from __future__ import annotations

import http.client
import json
import mimetypes
import os
import socket
import threading

from .identity import current

# Matches the daemon's --agent-event-sock default and the AGENT_EVENT_SOCK the
# launcher already exports. Env override kept for parity with the daemon flag.
_SOCK = os.environ.get("AGENT_EVENT_SOCK", "/run/aistackworks/agent.sock")
_TIMEOUT_S = 5.0
# Demo recordings are larger and the relay hop to MC is slower, so give the
# media upload its own (longer) timeout.
_MEDIA_TIMEOUT_S = 60.0


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


def _upload_asset(sock_path: str, card_id: str, event_key: str, asset_path: str,
                  timeout: float) -> str | None:
    """Upload ``asset_path`` to MC via the daemon UDS; return the hosted URL.

    POSTs the raw bytes to ``/v1/agent-media`` with the card identity, the
    inferred content type, and the filename in headers (the daemon wraps it as
    multipart for MC and adds the MC bearer). Returns the ``url`` MC assigns, or
    ``None`` on any failure (best-effort by contract)."""
    try:
        with open(asset_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None

    filename = os.path.basename(asset_path) or "asset"
    content_type = mimetypes.guess_type(asset_path)[0] or "application/octet-stream"
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
            },
        )
        resp = conn.getresponse()
        raw = resp.read()
        if not 200 <= resp.status < 300:
            return None
        return (json.loads(raw or b"{}") or {}).get("url")
    except (OSError, ValueError):
        return None
    finally:
        conn.close()

# Track which (card_id, skill) milestones the current session has already
# emitted, so the Kanban-transition backstop hook only fires when the skill
# itself didn't. Reset per session by the on_session_start hook.
_emitted_lock = threading.Lock()
_emitted: set[tuple[str, str]] = set()


def mark_emitted(card_id: str, skill: str) -> None:
    with _emitted_lock:
        _emitted.add((card_id, skill))


def already_emitted(card_id: str, skill: str) -> bool:
    with _emitted_lock:
        return (card_id, skill) in _emitted


def reset_emitted() -> None:
    with _emitted_lock:
        _emitted.clear()


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
    # body and pass it here so the event resolves to the right card in MC.
    ident_card, ident_session = current()
    card_id = (args.get("card_id") or "").strip() or ident_card
    if not card_id:
        # No dispatch identity captured (e.g. invoked outside an MC dispatch).
        return json.dumps({"ok": False, "reason": "no card identity for this session"})
    # ``session_id`` is stored/displayed by MC; keep the session-derived id when
    # present (the task id for workers) for traceability. The ``event_key`` MC
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

    it = args.get("iter")
    iter_part = "" if it is None else str(it)
    # Canonical card-based event_key so a skill's live call and the worker-exit
    # backstop produce the SAME key for one (card, skill, iter, status) and MC
    # dedups them (it is also the asset upload's X-Event-Key below).
    event_key = f"{event_session}:{skill}:{iter_part}:{status}"

    artifact = args.get("artifact")
    # Produced asset (e.g. a /demo recording): upload it to MC via the daemon and
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
        "fields": args.get("fields") or {},
        "sections": args.get("sections") or [],
        "artifact": artifact,
        "event_key": event_key,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    try:
        code = _post_uds(_SOCK, "/v1/agent-event", body, _TIMEOUT_S)
    except OSError as exc:
        return json.dumps({"ok": False, "reason": f"daemon unreachable: {exc}"})

    ok = 200 <= code < 300
    if ok:
        mark_emitted(card_id, skill)
    return json.dumps({"ok": ok, "status": code, "event_key": payload["event_key"]})
