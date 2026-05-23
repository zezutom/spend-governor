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
```

`telemetry.py` reads `PHOENIX_API_KEY_OBSERVED_WRITE` and sets it
as `PHOENIX_API_KEY` for the OTEL exporter. The two-key naming
reserves room for a separate Accountant read key.

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

### Run the agent once

```bash
uv run python -m observed.main "I want a refund for last month's charge."
```

Prints the tool sequence and the agent's reply. Useful for
eyeballing a single trace's behavior. The trace is emitted to
Phoenix in the background.

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
uv run python -m accountant.inspect_traces --since 1h --show 20
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
uv run python -m accountant.verify_cost
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
