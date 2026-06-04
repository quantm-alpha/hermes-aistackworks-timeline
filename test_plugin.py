"""Self-contained tests for the aistackworks-timeline plugin.

No Hermes runtime required — exercises identity capture, payload shape and the
real Unix-socket transport against a throwaway in-process daemon.

Run:  python -m unittest containerization.hermes.plugins.aistackworks-timeline.test_plugin
  or: cd containerization/hermes/plugins/aistackworks-timeline && python -m unittest test_plugin
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import unittest

# The plugin uses package-relative imports (how Hermes loads it). The directory
# name has a hyphen, so register a synthetic package pointing at this dir to make
# those relative imports resolve when running the tests standalone.
_HERE = pathlib.Path(__file__).resolve().parent
_PKG = "asw_timeline_under_test"
if _PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _PKG, _HERE / "__init__.py", submodule_search_locations=[str(_HERE)]
    )
    sys.modules[_PKG] = importlib.util.module_from_spec(_spec)
identity = importlib.import_module(f"{_PKG}.identity")
tools = importlib.import_module(f"{_PKG}.tools")


class _FakeDaemon:
    """Accepts one UDS connection, captures the POST body, replies 202."""

    def __init__(self, sock_path: str):
        self.sock_path = sock_path
        self.captured: bytes | None = None
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn, _ = self._srv.accept()
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            header, _, rest = data.partition(b"\r\n\r\n")
            length = 0
            for line in header.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
            body = rest
            while len(body) < length:
                body += conn.recv(4096)
            self.captured = body
            conn.sendall(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n")

    def close(self) -> None:
        self._srv.close()


class _MultiDaemon:
    """Serves many UDS connections, capturing every POST body (the worker-exit
    backstop emits one event per stage). report_progress posts synchronously, so
    by the time the backstop returns all bodies are captured."""

    def __init__(self, sock_path: str):
        self.sock_path = sock_path
        self.bodies: list[bytes] = []
        self._stop = False
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(8)
        self._srv.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                header, _, rest = data.partition(b"\r\n\r\n")
                length = 0
                for line in header.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                body = rest
                while len(body) < length:
                    body += conn.recv(4096)
                self.bodies.append(body)
                conn.sendall(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n")

    def events(self) -> list:
        return [json.loads(b) for b in self.bodies]

    def by_skill(self) -> dict:
        return {(e["skill"], e["status"]): e for e in self.events()}

    def close(self) -> None:
        self._stop = True
        self._thread.join(timeout=2)
        self._srv.close()


class IdentityTests(unittest.TestCase):
    def test_derives_card_from_card_prefix(self):
        identity.remember("card-36fc-abc")
        card, session = identity.current()
        self.assertEqual(card, "36fc-abc")
        self.assertEqual(session, "card-36fc-abc")

    def test_derives_card_from_plan_card_prefix(self):
        identity.remember("plan-card-xyz")
        card, _ = identity.current()
        self.assertEqual(card, "xyz")

    def test_blank_session_is_ignored(self):
        identity.remember("card-keep")
        identity.remember("")
        card, _ = identity.current()
        self.assertEqual(card, "keep")


class ReportProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sock = os.path.join(self.tmp, "agent.sock")
        self.daemon = _FakeDaemon(self.sock)
        # Point the handler at the fake daemon.
        self._orig_sock = tools._SOCK
        tools._SOCK = self.sock

    def tearDown(self):
        tools._SOCK = self._orig_sock
        self.daemon.close()

    def test_emits_well_formed_payload(self):
        identity.remember("card-deadbeef")
        result = json.loads(
            tools.report_progress(
                {
                    "skill": "build",
                    "status": "pass",
                    "headline": "Build complete",
                    "iter": 2,
                    "fields": {"Files changed": "14"},
                    "artifact": {"kind": "commit", "label": "sha 7d2b1f8"},
                }
            )
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], 202)

        self.daemon._thread.join(timeout=2)
        sent = json.loads(self.daemon.captured)
        self.assertEqual(sent["card_id"], "deadbeef")
        self.assertEqual(sent["session_id"], "card-deadbeef")
        self.assertEqual(sent["skill"], "build")
        self.assertEqual(sent["status"], "pass")
        self.assertEqual(sent["iter"], 2)
        self.assertEqual(sent["event_key"], "card-deadbeef:build:2:pass")
        self.assertEqual(sent["artifact"]["kind"], "commit")

    def test_explicit_card_id_overrides_subtask_session(self):
        # Coder/reviewer/ship run as spawned Kanban tasks whose session id is the
        # task id, NOT card-<notion_page_id>. The explicit card_id arg must win so
        # the event still resolves to the right card in MC.
        identity.remember("kanban-task-7e3f")
        result = json.loads(
            tools.report_progress(
                {
                    "card_id": "deadbeef",
                    "skill": "build",
                    "status": "ready_for_test",
                    "headline": "Build complete",
                    "iter": 1,
                }
            )
        )
        self.assertTrue(result["ok"], result)
        self.daemon._thread.join(timeout=2)
        sent = json.loads(self.daemon.captured)
        self.assertEqual(sent["card_id"], "deadbeef")
        # session_id stays the subtask's own (for traceability)…
        self.assertEqual(sent["session_id"], "kanban-task-7e3f")
        # …but the event_key MC dedups on is CANONICAL card-based, so a skill's
        # live call and the worker-exit backstop produce the same key for one
        # (card, skill, iter, status) and never double-post the milestone.
        self.assertEqual(sent["event_key"], "card-deadbeef:build:1:ready_for_test")

    def test_explicit_card_id_synthesizes_session_when_no_identity(self):
        # No session identity captured at all (hook never fired): synthesize the
        # canonical card-<id> session so the event_key stays well-formed.
        identity._current["card_id"] = ""
        identity._current["session_id"] = ""
        result = json.loads(
            tools.report_progress(
                {"card_id": "cafe1234", "skill": "ship", "status": "shipped", "headline": "Shipped"}
            )
        )
        self.assertTrue(result["ok"], result)
        self.daemon._thread.join(timeout=2)
        sent = json.loads(self.daemon.captured)
        self.assertEqual(sent["card_id"], "cafe1234")
        self.assertEqual(sent["session_id"], "card-cafe1234")
        self.assertEqual(sent["event_key"], "card-cafe1234:ship::shipped")

    def test_missing_identity_is_a_noop(self):
        # Reset identity to empty.
        identity._current["card_id"] = ""
        identity._current["session_id"] = ""
        result = json.loads(
            tools.report_progress({"skill": "build", "status": "pass", "headline": "x"})
        )
        self.assertFalse(result["ok"])
        self.assertIn("identity", result["reason"])

    def test_missing_required_field_is_rejected(self):
        identity.remember("card-1")
        result = json.loads(tools.report_progress({"skill": "build", "status": "pass"}))
        self.assertFalse(result["ok"])


import sqlite3

# Execute the package __init__ (the top-of-file synthetic package was registered
# for relative-import resolution but not exec'd) to get the hook + helpers.
plugin = sys.modules[_PKG]
if not hasattr(plugin, "_on_post_tool_call"):
    plugin.__spec__.loader.exec_module(plugin)


def _make_kanban_db(path: str, rows: list[tuple], runs: "list[tuple] | None" = None) -> None:
    """rows: (id, title, body, skills_json_or_None[, status]).
    runs: (task_id, summary, metadata_json, outcome, status, ended_at) for the
    rich-backstop path (the worker-exit event is built from task_runs)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, skills TEXT, status TEXT)")
    norm = [r if len(r) == 5 else (*r, "running") for r in rows]
    conn.executemany("INSERT INTO tasks (id,title,body,skills,status) VALUES (?,?,?,?,?)", norm)
    conn.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, "
        "summary TEXT, metadata TEXT, outcome TEXT, status TEXT, "
        "started_at INTEGER, ended_at INTEGER)"
    )
    for tid, summary, meta, outcome, status, ended in (runs or []):
        conn.execute(
            "INSERT INTO task_runs (task_id,summary,metadata,outcome,status,started_at,ended_at) "
            "VALUES (?,?,?,?,?,?,?)", (tid, summary, meta, outcome, status, ended, ended),
        )
    conn.commit()
    conn.close()


class KanbanIdentityTests(unittest.TestCase):
    """A worker session id is the task id, not card-<id>; the card must come
    from the task body via the Kanban DB."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "kanban.db")
        _make_kanban_db(self.db, [
            ("t_coder", "E2E: add tagline", "card_id: e2ecard0001\n\nCard title: ...",
             '["build", "test", "demo"]'),
            ("t_ship", "Ship: E2E: add tagline", "card_id: e2ecard0001\n\nRun /ship...", None),
        ])
        self._env = {k: os.environ.get(k) for k in ("HERMES_KANBAN_DB", "HERMES_KANBAN_TASK")}
        os.environ["HERMES_KANBAN_DB"] = self.db

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_read_task_parses_card_id_and_skills(self):
        meta = identity.read_task("t_coder")
        self.assertEqual(meta["card_id"], "e2ecard0001")
        self.assertEqual(meta["skills"], ["build", "test", "demo"])

    def test_worker_session_resolves_card_from_task_body(self):
        os.environ["HERMES_KANBAN_TASK"] = "t_coder"
        identity.remember("t_coder")  # session id is the task id, NOT card-<id>
        card, session = identity.current()
        self.assertEqual(card, "e2ecard0001")  # from the body, not the session
        self.assertEqual(session, "t_coder")


class BackstopHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sock = os.path.join(self.tmp, "agent.sock")
        self.daemon = _MultiDaemon(self.sock)
        self._orig_sock = tools._SOCK
        tools._SOCK = self.sock
        self.db = os.path.join(self.tmp, "kanban.db")
        _make_kanban_db(self.db, [
            # (id, title, body, skills, status)
            ("t_coder", "E2E: add tagline", "card_id: cardX\n\n...", '["build", "test", "demo"]', "running"),
            ("t_ship", "Ship: E2E: add tagline", "card_id: cardX\n\nRun /ship...", None, "running"),
            ("t_rev", "Review: E2E: add tagline", "card_id: cardX\n\nReview...", None, "running"),
            ("t_blocked", "E2E: add tagline", "card_id: cardX\n\n...", '["build", "test", "demo"]', "blocked"),
        ])
        self._env = {k: os.environ.get(k) for k in ("HERMES_KANBAN_DB", "HERMES_KANBAN_TASK")}
        os.environ["HERMES_KANBAN_DB"] = self.db
        os.environ.pop("HERMES_KANBAN_TASK", None)
        tools.reset_emitted()

    def tearDown(self):
        tools._SOCK = self._orig_sock
        self.daemon.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_coder_session_end_emits_all_stage_milestones(self):
        os.environ["HERMES_KANBAN_TASK"] = "t_coder"
        plugin._on_session_finalize(session_id="t_coder")
        sent = self.daemon.by_skill()
        # All three stages the coder task covered are emitted, in order.
        self.assertIn(("build", "ready_for_test"), sent)
        self.assertIn(("test", "awaiting_demo"), sent)
        self.assertIn(("demo", "pass"), sent)
        self.assertTrue(all(e["card_id"] == "cardX" for e in self.daemon.events()))

    def test_backstop_fills_only_stages_the_skill_skipped(self):
        os.environ["HERMES_KANBAN_TASK"] = "t_coder"
        tools.mark_emitted("cardX", "build")  # skill reported build + test live
        tools.mark_emitted("cardX", "test")
        plugin._on_session_finalize(session_id="t_coder")
        sent = self.daemon.by_skill()
        # Only the un-emitted demo stage is backstopped; no build/test duplicate.
        self.assertEqual(list(sent), [("demo", "pass")])

    def test_backstop_skips_when_all_stages_already_emitted(self):
        os.environ["HERMES_KANBAN_TASK"] = "t_coder"
        for s in ("build", "test", "demo"):
            tools.mark_emitted("cardX", s)  # skill reported every stage live
        plugin._on_session_finalize(session_id="t_coder")
        self.assertEqual(self.daemon.events(), [])  # no duplicates

    def test_ship_title_maps_to_ship_shipped(self):
        os.environ["HERMES_KANBAN_TASK"] = "t_ship"
        plugin._on_session_finalize(session_id="t_ship")
        self.assertEqual(list(self.daemon.by_skill()), [("ship", "shipped")])

    def test_blocked_task_does_not_emit_success(self):
        os.environ["HERMES_KANBAN_TASK"] = "t_blocked"
        plugin._on_session_finalize(session_id="t_blocked")
        self.assertEqual(self.daemon.events(), [])  # blocked → no optimistic success

    def test_explicit_kanban_block_emits_blocked(self):
        plugin._on_post_tool_call(tool_name="kanban_block", task_id="t_coder")
        sent = self.daemon.events()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["status"], "blocked")

    def test_reviewer_task_is_not_advanced(self):
        os.environ["HERMES_KANBAN_TASK"] = "t_rev"
        plugin._on_session_finalize(session_id="t_rev")
        self.assertEqual(self.daemon.events(), [])  # reviewer has no MC mapping

    def test_non_worker_session_does_not_emit(self):
        os.environ.pop("HERMES_KANBAN_TASK", None)  # main's refine dispatch, not a worker
        plugin._on_session_finalize(session_id="card-cardX")
        self.assertEqual(self.daemon.events(), [])

    def test_non_kanban_tool_is_ignored(self):
        plugin._on_post_tool_call(tool_name="write_file", task_id="t_coder")
        self.assertEqual(self.daemon.events(), [])


class RichBackstopTests(unittest.TestCase):
    """The worker-exit backstop builds a RICH event from the task's run record
    (summary + metadata the skill wrote to kanban_complete), not a stub line."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sock = os.path.join(self.tmp, "agent.sock")
        self.daemon = _MultiDaemon(self.sock)
        self._orig_sock = tools._SOCK
        tools._SOCK = self.sock
        self.db = os.path.join(self.tmp, "kanban.db")
        _make_kanban_db(
            self.db,
            [("t_coder", "Add tagline", "card_id: cardX\n\n...", '["build", "test", "demo"]', "running")],
            runs=[(
                "t_coder",
                "Added a Quick Start section to README.md; PR opened, checks pass.",
                json.dumps({
                    "changed_files": ["README.md", ".aistackworks/cards/card-cardX.tasks.md"],
                    "branch": "feat/cardX-quick-start",
                    "pr_url": "https://github.com/o/r/pull/9",
                    # exercise the Codex {"item": [...]} wrapper + mixed casing
                    "commits": {"item": ["5c48765", "6ebd58d"]},
                    "TESTS_RUN": ["readme acceptance"],
                }),
                "completed", "done", 1000,
            )],
        )
        self._env = {k: os.environ.get(k) for k in ("HERMES_KANBAN_DB", "HERMES_KANBAN_TASK")}
        os.environ["HERMES_KANBAN_DB"] = self.db
        os.environ["HERMES_KANBAN_TASK"] = "t_coder"
        tools.reset_emitted()

    def tearDown(self):
        tools._SOCK = self._orig_sock
        self.daemon.close()
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_read_latest_run_parses_summary_and_metadata(self):
        run = identity.read_latest_run("t_coder")
        self.assertIn("Quick Start", run["summary"])
        self.assertEqual(run["metadata"]["branch"], "feat/cardX-quick-start")
        self.assertEqual(run["outcome"], "completed")

    def test_backstop_emits_rich_event_from_run_record(self):
        plugin._on_session_finalize(session_id="t_coder")
        by = self.daemon.by_skill()
        # Every stage is emitted; each carries its own clean framing title.
        self.assertEqual(by[("build", "ready_for_test")]["headline"], "What was built")
        self.assertEqual(by[("test", "awaiting_demo")]["headline"], "QA results")
        sent = by[("demo", "pass")]
        # Demo headline is a clean framing TITLE; the verbose run summary moves to
        # a body section (not dumped as the title).
        self.assertEqual(sent["headline"], "What was done")
        self.assertTrue(any("Quick Start" in s.get("body", "") for s in sent["sections"]),
                        "run summary should appear in a section body")
        # Rich fields pulled from metadata, incl. unwrapped {"item": [...]} list.
        self.assertEqual(sent["fields"]["PR_URL"], "https://github.com/o/r/pull/9")
        self.assertEqual(sent["fields"]["BRANCH"], "feat/cardX-quick-start")
        self.assertEqual(sent["fields"]["COMMITS"], "5c48765, 6ebd58d")
        self.assertIn("README.md", sent["fields"]["FILES"])
        self.assertEqual(sent["artifact"]["kind"], "pr")
        # Canonical card-based event_key → dedups with a skill's live tool call.
        self.assertEqual(sent["event_key"], "card-cardX:demo::pass")


class _RoutingDaemon:
    """UDS daemon that serves multiple requests and routes by path:
    ``/v1/agent-media`` → 201 + JSON ``{id,url}``; anything else → 202. Records
    the last body + headers seen per path."""

    def __init__(self, sock_path: str, media_url: str):
        self.sock_path = sock_path
        self.media_url = media_url
        self.bodies: dict[str, bytes] = {}
        self.headers: dict[str, dict[str, str]] = {}
        self._stop = False
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(8)
        self._srv.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @staticmethod
    def _read_request(conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        path = lines[0].split(b" ")[1].decode() if lines and lines[0] else ""
        hdrs = {}
        for line in lines[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                hdrs[k.strip().lower().decode()] = v.strip().decode()
        body = rest
        length = int(hdrs.get("content-length", "0"))
        while len(body) < length:
            body += conn.recv(4096)
        return path, hdrs, body

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                try:
                    path, hdrs, body = self._read_request(conn)
                except OSError:
                    continue
                self.bodies[path] = body
                self.headers[path] = hdrs
                if path == "/v1/agent-media":
                    payload = json.dumps({"id": "vid-1", "url": self.media_url}).encode()
                    conn.sendall(
                        b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
                        b"Content-Length: %d\r\n\r\n" % len(payload) + payload
                    )
                else:
                    conn.sendall(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n")

    def close(self):
        self._stop = True
        self._thread.join(timeout=2)
        self._srv.close()


class AssetUploadTests(unittest.TestCase):
    MEDIA_URL = "/api/cards/deadbeef/videos/vid-1/"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sock = os.path.join(self.tmp, "agent.sock")
        self.daemon = _RoutingDaemon(self.sock, self.MEDIA_URL)
        self._orig_sock = tools._SOCK
        tools._SOCK = self.sock
        self.asset = os.path.join(self.tmp, "page@abc.mp4")
        self.asset_bytes = b"\x00\x01fake-mp4-bytes" * 256
        with open(self.asset, "wb") as fh:
            fh.write(self.asset_bytes)

    def tearDown(self):
        tools._SOCK = self._orig_sock
        self.daemon.close()

    def test_asset_uploads_and_sets_artifact_href(self):
        identity.remember("card-deadbeef")
        result = json.loads(
            tools.report_progress(
                {
                    "skill": "demo",
                    "status": "pass",
                    "headline": "Demo recorded",
                    "asset": self.asset,
                    "artifact": {"kind": "demo", "label": "page@abc.mp4"},
                }
            )
        )
        self.assertTrue(result["ok"], result)
        # The media endpoint received the raw bytes + card/filename/type headers.
        self.assertIn("/v1/agent-media", self.daemon.bodies)
        self.assertEqual(self.daemon.bodies["/v1/agent-media"], self.asset_bytes)
        media_headers = self.daemon.headers["/v1/agent-media"]
        self.assertEqual(media_headers["x-card-id"], "deadbeef")
        # Canonical card-based event_key (this branch unifies it across tool +
        # CLI + backstop), which is what the asset upload tags the media with.
        self.assertEqual(media_headers["x-event-key"], "card-deadbeef:demo::pass")
        self.assertEqual(media_headers["x-filename"], "page@abc.mp4")
        self.assertEqual(media_headers["content-type"], "video/mp4")
        # The emitted timeline event points the artifact at the hosted URL.
        sent = json.loads(self.daemon.bodies["/v1/agent-event"])
        self.assertEqual(sent["artifact"]["kind"], "demo")
        self.assertEqual(sent["artifact"]["href"], self.MEDIA_URL)
        self.assertEqual(sent["artifact"]["label"], "page@abc.mp4")

    def test_failed_upload_keeps_passed_artifact(self):
        # A missing file → upload returns None before connecting → the artifact
        # the caller passed is emitted unchanged, and no media POST is made.
        identity.remember("card-deadbeef")
        result = json.loads(
            tools.report_progress(
                {
                    "skill": "demo",
                    "status": "pass",
                    "headline": "Demo recorded",
                    "asset": os.path.join(self.tmp, "missing.mp4"),
                    "artifact": {"kind": "demo", "label": "x", "href": "local/path.mp4"},
                }
            )
        )
        self.assertTrue(result["ok"], result)
        self.assertNotIn("/v1/agent-media", self.daemon.bodies)
        sent = json.loads(self.daemon.bodies["/v1/agent-event"])
        self.assertEqual(sent["artifact"]["href"], "local/path.mp4")


if __name__ == "__main__":
    unittest.main()
