# Development

Running and modifying the project locally.

## Prerequisites

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** for dependency management
- **`gcloud` CLI** authenticated against a Google Cloud project
  with billing enabled and Vertex AI enabled
- A **Phoenix Cloud** account with an API key

## Setup

```bash
git clone <repo>
cd agent-accountant
uv sync
```

That installs everything from `pyproject.toml` into a virtualenv at
`.venv/`. From here on, `uv run <command>` resolves through that
virtualenv.

## Environment variables

Create `.env` in the project root:

```
PHOENIX_COLLECTOR_ENDPOINT=https://app.phoenix.arize.com/s/<your-tenant>
PHOENIX_PROJECT_NAME=agent-accountant
PHOENIX_API_KEY_OBSERVED_WRITE=<your-phoenix-api-key>

GOOGLE_GENAI_USE_VERTEXAI=True
GOOGLE_CLOUD_PROJECT=<your-gcp-project-id>
GOOGLE_CLOUD_LOCATION=us-central1

# Enables the observed agent's real-time fan-out to the Governor.
# Without it, spans go only to Phoenix (and the agent logs a notice).
GOVERNOR_INGEST_URL=http://localhost:8765
```

`telemetry.py` reads `PHOENIX_API_KEY_OBSERVED_WRITE` and sets it
as `PHOENIX_API_KEY` for the OTEL exporter. The two-key naming
reserves room for a separate Governor read key.

`GOVERNOR_INGEST_URL` turns on the second OTEL exporter that posts
spans to the Governor ingest server in real time. Leave it unset to
emit to Phoenix only.

## Vertex AI authentication

Vertex AI uses Application Default Credentials (ADC), not an API
key. Two non-obvious steps:

1. **Log in:**
   ```bash
   gcloud auth application-default login
   ```
2. **Set the quota project on ADC:**
   ```bash
   gcloud auth application-default set-quota-project <your-gcp-project-id>
   ```

Step 2 is the trap. `GOOGLE_CLOUD_PROJECT` in `.env` tells the
genai SDK which project to *target*. The quota project is a
*separate* attribute on ADC that determines which project gets
billed for quota. Without it, calls fall through to AI Studio
free-tier quota even though the SDK is hitting Vertex AI — you
will hit 429s a few dozen calls in, with a warning like:

> Your application has authenticated using end user credentials
> from Google Cloud SDK without a quota project.

If you see that warning, run the `set-quota-project` command and
try again.

## Common commands

### Launch the cockpit (the main entry point)

```bash
./scripts/start-cockpit.sh
```

This one command boots the whole stack: the control-plane API (`:8800`),
the trace-ingest server (`:8765`), and the React cockpit (Vite, `:5173`).
**Open http://localhost:5173.** On first run it imports history from
Phoenix (empty cache = new account). See
[realtime-pipeline.md](./realtime-pipeline.md) for what happens under
the hood.

To feed it live traffic, run the observed agent (next command) with
`GOVERNOR_INGEST_URL` set — the dashboard reflects each new trace
within ~0.5s.

The ingest server can also be started on its own (e.g. for debugging):

```bash
uv run uvicorn governor.pipeline.ingest_server:app --port 8765
```

### Run the observed agent once

```bash
GOVERNOR_INGEST_URL=http://localhost:8765 \
  uv run python -m observed.main "I want a refund for last month's charge."
```

Prints the tool sequence and the agent's reply. The trace is emitted
to Phoenix and (with the env var set) to the Governor in the
background.

### Generate a synthetic dataset

```bash
uv run python -m observed.generate_dataset 20 5
```

Runs 20 agent invocations at concurrency 5. The task mix is
weighted (35% password_reset, 25% account_question, 25%
refund_handling, 15% plan_change). Concurrency 5–10 keeps Vertex's
per-minute quota comfortable in `us-central1`.

### Inspect traces

```bash
uv run python -m governor.cli.inspect_traces --since 1h --show 20
```

Pulls spans from Phoenix, groups by trace, prints per-trace tool
sequence + cost and aggregates by task class.

Options:

- `--since 30m|4h|7d` — only traces newer than this window
- `--show N` — number of most-recent traces to list (default 20)
- `--limit N` — span ceiling per Phoenix request (default 2000)

Pulling more than a few thousand spans in one request can trigger
server-side disconnects from Phoenix Cloud. When `--since` is set,
the script chunks the time range into 10-minute slices, walking
newest → oldest so a mid-pull failure still leaves the most recent
traces in hand.

### Verify the cost computation

```bash
uv run python -m governor.cli.verify_cost
```

Runs `cost.py` against a hand-picked `usage_metadata` and prints
the breakdown. Use as a regression check after pricing changes.

## Troubleshooting

### Vertex 429s after switching from API key

See the "Vertex AI authentication" section above. Almost always
the ADC quota project isn't set.

### Phoenix `ReadTimeout` or `RemoteProtocolError`

Phoenix Cloud disconnects on large single-response span pulls. Use
a smaller `--since` window with `inspect_traces`, or rely on the
chunked path (it activates automatically when `--since` is set).

### Empty traces in Phoenix

Check that `PHOENIX_API_KEY_OBSERVED_WRITE` is set in `.env` and
that the Phoenix project name matches. The ADK instrumentor needs
`init_telemetry()` to run *before* `build_agent()` imports — see
the import order in `observed/main.py` and
`observed/generate_dataset.py`.

### Dashboard shows nothing / counters stuck at zero

- Confirm the ingest server is up: `curl http://localhost:8765/health`.
  The dashboard spawns it automatically, but if port 8765 is taken by
  another process the spawn is skipped.
- For live traffic, confirm the observed agent ran with
  `GOVERNOR_INGEST_URL` set — without it, spans only reach Phoenix.
  The agent prints a notice on startup either way.
- The dashboard reads SQLite at `data/accountant.db`. Deleting it
  forces a fresh new-account backfill on the next dashboard load.

### Reset to a clean "new account" state

```bash
rm -f data/accountant.db data/accountant.db-wal data/accountant.db-shm
```

Next dashboard load sees an empty cache and re-runs the Phoenix
backfill from scratch. The cache is gitignored and disposable.
