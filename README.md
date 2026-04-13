# OpenClaw Hub

Task orchestration server for AI agent pipelines. Manages task lifecycle, dispatches work to agents, runs automated code review cycles, and provides a web dashboard.

## Features

- **Task hierarchy**: Epic → Feature → Task → Subtask
- **Full lifecycle**: draft → open → running → review → completed (with CI checks, arbiter, Q&A)
- **Plugin architecture**: integrations (dispatch, git_ops, GitHub, Vast.ai, notes) are pluggable
- **Web dashboard**: HTMX-powered UI with inbox, kanban, task detail, log viewer
- **MCP server**: Model Context Protocol tools for Cursor/remote agents
- **CLI**: `oc-hub` command for agents and humans
- **Background poller**: auto-sync with dispatch jobs, stale detection, review dispatch

## Quick Start

```bash
# Clone
git clone https://github.com/mrPDA/openclaw-hub.git
cd openclaw-hub

# Install
uv venv && uv pip install -e .

# Run
openclaw-hub
# → http://localhost:8080
```

## Configuration

All configuration via environment variables:

| Variable | Default | Description |
|---|---|---|
| `OPENCLAW_HUB_REPO` | `""` | GitHub repo (e.g. `owner/repo`) for PR/commit integration |
| `OPENCLAW_WORKSPACE_REPO` | `~/.openclaw/workspace/repo` | Path to the workspace git repo |
| `OPENCLAW_DISPATCH_BIN` | `~/.local/bin/oc-dev-dispatch` | Path to dispatch binary |
| `OPENCLAW_HUB_DB` | `~/.local/state/openclaw-hub/hub.db` | SQLite database path |
| `OPENCLAW_HUB_HOST` | `0.0.0.0` | Server bind host |
| `OPENCLAW_HUB_PORT` | `8080` | Server bind port |
| `OPENCLAW_TRANSCRIPTS_DIR` | `~/.openclaw/transcripts` | Agent transcript directory |
| `OPENCLAW_N4L_BIN` | `~/.local/bin/n4l` | notesforllm CLI path |
| `OPENCLAW_N4L_SPACE` | `""` | notesforllm space ID |
| `OPENCLAW_VAST_JOB_BIN` | `~/.local/bin/vast-openclaw` | Vast.ai CLI path |
| `GH_BIN` | `gh` | GitHub CLI binary |
| `OPENCLAW_MAX_REVIEW_CYCLES` | `3` | Max automated review cycles |
| `OPENCLAW_MAX_CI_FIX_CYCLES` | `3` | Max CI fix attempts |
| `OPENCLAW_STALE_MINUTES` | `30` | Minutes before a task is flagged stale |

## Plugin System

Hub uses a plugin architecture for external integrations. Each integration implements a `typing.Protocol` and is registered at startup.

**Bundled plugins** (auto-registered when binaries exist):
- `DispatchPlugin` — task dispatch via `oc-dev-dispatch`
- `GitOpsPlugin` — git branch/PR/merge via local git + `gh` CLI
- `GitHubPlugin` — commits/PRs via `gh` CLI
- `NotesPlugin` — decisions via `n4l` CLI
- `VastPlugin` — GPU instance management via `vast-openclaw`
- `TranscriptsPlugin` — agent transcript viewer

**Without plugins**: Hub starts with noop implementations — all features work, integrations gracefully return empty data.

**Custom plugins**: implement the protocol from `hub/integrations/protocols.py` and register in `app.py` lifespan.

## Development

```bash
# Install with dev dependencies
uv pip install -e . && uv pip install pytest pytest-asyncio pytest-cov ruff

# Tests
.venv/bin/pytest tests/ -q

# Lint
.venv/bin/ruff check hub/ tests/
.venv/bin/ruff format hub/ tests/
```

## Use as Submodule

```bash
# In your project
git submodule add git@github.com:mrPDA/openclaw-hub.git hub
git submodule update --init --recursive

# Install
cd hub && uv venv && uv pip install -e .
```

## Architecture

```
hub/
├── app.py              # FastAPI app, lifespan, REST API routes
├── web.py              # HTMX/HTML web routes
├── services/           # Business logic (lifecycle, orchestration, dashboard)
├── repository.py       # SQL data access layer
├── db.py               # Schema, migrations, hierarchy helpers
├── models.py           # Pydantic models and enums
├── poller.py           # Background task sync
├── config.py           # Environment-based configuration
├── cli.py              # oc-hub CLI
├── mcp_server.py       # MCP tools for agents
├── integrations/
│   ├── protocols.py    # Plugin protocol definitions
│   ├── noop.py         # No-op (null) implementations
│   ├── registry.py     # Central plugin registry
│   ├── dispatch.py     # oc-dev-dispatch integration
│   ├── git_ops.py      # Git + GitHub operations
│   ├── github.py       # GitHub API (commits, PRs)
│   ├── notes.py        # notesforllm bridge
│   ├── vast.py         # Vast.ai management
│   └── transcripts.py  # Agent transcript reader
├── templates/          # Jinja2 templates
├── static/             # CSS
└── tests/              # pytest test suite (84 tests)
```

## License

MIT
