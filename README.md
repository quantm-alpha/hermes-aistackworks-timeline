# aistackworks-timeline — Hermes plugin

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin that emits
per-card **skill timeline events** (refine / build / test / demo / ship) to
[AIStackWorks](https://aistackworks.com) so a dispatched agent's progress shows
up live on the card.

It exposes a `report_progress` tool that skills call at each milestone, plus a
deterministic **worker-exit backstop** that emits any stage the skill didn't
report itself. Events are POSTed to the local AIStackWorks agent-host daemon over
its Unix socket — no tokens, no network egress from the agent.

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

The plugin POSTs to the agent-host daemon's Unix socket. Override the path if
your deployment differs from the default:

| Env var | Default | Purpose |
| --- | --- | --- |
| `AGENT_EVENT_SOCK` | `/run/aistackworks/agent.sock` | The agent-host daemon socket the plugin POSTs timeline events to (`/v1/agent-event`). |

Without a reachable socket the agent still runs; it just emits no timeline
events.

## Layout

Flat plugin package — the repo root *is* the plugin (`hermes plugins install`
clones it into `~/.hermes/plugins/aistackworks-timeline/`):

```
plugin.yaml      manifest (name, version, kind: standalone, provided tools/hooks)
__init__.py      register(ctx) — tool + session hooks + worker-exit backstop
schemas.py       report_progress tool schema
tools.py         report_progress handler (UDS POST) + emitted-state tracking
identity.py      per-session card-id capture (session hooks + Kanban DB)
test_plugin.py   unit tests (stdlib unittest)
```

## Test

```sh
python -m unittest test_plugin -v
```
