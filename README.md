# aistackworks-timeline — Hermes plugin

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin that emits
per-card **skill timeline events** (refine / build / test / demo / ship) to
[AIStackWorks](https://aistackworks.com) so a dispatched agent's progress shows
up live on the card.

It exposes a `report_progress` tool that skills call at each milestone, plus a
deterministic **worker-exit backstop** that emits any stage the skill didn't
report itself.

## Transport

Events reach AIStackWorks over one of two legs, tried in this order:

1. **AIStackWorks MCP server (preferred).** When the profile env carries
   `MC_MCP_URL` and `MC_MCP_TOKEN`, the event is sent as a JSON-RPC `tools/call`
   of AIStackWorks' `report_progress` MCP tool — a direct, bearer-authenticated
   HTTPS call to the stateless streamable-HTTP endpoint (`/api/mcp/`).
2. **Agent-host daemon Unix socket (fallback).** Otherwise — or if the MCP call
   fails for any reason — the event is POSTed to the local AIStackWorks
   agent-host daemon over its Unix socket (`/v1/agent-event`), which relays it to
   AIStackWorks over its existing outbound WebSocket. This leg needs no token and
   makes no network egress from the agent.

Both transports converge on the same card timeline and share one canonical event
key, so an event emitted over either leg is idempotent against the other.

Produced **assets** (e.g. a `/demo` recording) always upload over the agent-host
daemon's media socket (`/v1/agent-media`) regardless of which leg carries the
event — binary media does not belong on the JSON MCP call. The asset uploads
first; its hosted URL is then carried in the event.

## Install

The plugin is **opt-in per profile**. Two delivery paths, depending on who needs
to see the tool:

### 1. Via the Hermes CLI (umbrella gateway / interactive use)

```sh
hermes plugins install quantm-alpha/hermes-aistackworks-timeline --enable
```

This installs into `~/.hermes/plugins/` and enables it for the active profile —
good for a coordinator profile and interactive sessions.

> **Dispatched workers:** Hermes spawns workers with `HERMES_HOME=<profile dir>`,
> so a plugin installed only under the umbrella `~/.hermes` home is **not**
> discovered by those workers. For an autonomous agent team, deliver the plugin
> **per profile** (below).

### 2. Per profile (autonomous team / dispatched workers)

Drop the plugin into each profile's own `plugins/` dir and enable it in that
profile's `config.yaml`:

```yaml
plugins:
  enabled:
    - aistackworks-timeline
```

Installed inside the profile, it's discoverable from the worker's own
`HERMES_HOME`. AIStackWorks agent profile distributions bundle the plugin this
way, so installing a profile brings the plugin with it.

## Configure

The transport is selected from the environment — no config file:

| Env var | Default | Purpose |
| --- | --- | --- |
| `MC_MCP_URL` | _(unset)_ | AIStackWorks MCP endpoint (e.g. `https://app.aistackworks.com/api/mcp/?project=<slug>`). Set **with** `MC_MCP_TOKEN` to send events over MCP; the profile's `config.yaml` already interpolates these for its MCP server registration. |
| `MC_MCP_TOKEN` | _(unset)_ | Bearer token for the MCP endpoint. |
| `AGENT_EVENT_SOCK` | `/run/aistackworks/agent.sock` | The agent-host daemon socket the plugin POSTs timeline events to (`/v1/agent-event`), and uploads assets to (`/v1/agent-media`). The fallback when MCP is unset/failing, and always the asset-upload path. |

When `MC_MCP_URL` and `MC_MCP_TOKEN` are both set the plugin sends events over
MCP and falls back to the daemon socket on any MCP failure. When they are unset
it uses the daemon socket only. Without any reachable transport the agent still
runs; it just emits no timeline events.

## Layout

Flat plugin package — the repo root *is* the plugin (`hermes plugins install`
clones it into `~/.hermes/plugins/aistackworks-timeline/`):

```
plugin.yaml      manifest (name, version, kind: standalone, provided tools/hooks)
__init__.py      register(ctx) — tool + session hooks + worker-exit backstop
schemas.py       report_progress tool schema
tools.py         report_progress handler (MCP-first, UDS fallback) + emitted-state tracking
identity.py      per-session card-id capture (session hooks + Kanban DB)
test_plugin.py   unit tests (stdlib unittest)
```

## Test

```sh
python -m unittest test_plugin -v
```
