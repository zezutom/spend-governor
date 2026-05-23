# Agent Accountant

**Unit economics for AI agents — see what each customer, task, and outcome actually costs you.**

A submission to the [Google Cloud Rapid Agent Hackathon](https://rapid-agent.devpost.com/).

---

## What this does

AI agents call LLMs and tools. Each call has a cost. Most teams
running agents in production don't know what each *customer*,
*task*, or *outcome* actually costs them — only the total cloud
bill at the end of the month.

Agent Accountant reads traces from your existing observability
backend, attaches LLM and tool costs to every call, aggregates to
unit economics, finds where the money is going, and proposes
optimizations. On approval, it executes them against the observed
agent's configuration and re-measures.

A worked example from the demo: a customer-support agent costs
`$0.41` per resolved ticket on average. Refund tickets cost `$2.10`
each because the agent makes four redundant web searches per
request. The Accountant proposes caching the refund policy page and
routing post-first-hop reasoning to a cheaper model. On approval,
it writes the new config. Next batch of refund tickets: `$0.62` each.
Saved `$1.48` per refund.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  THE OBSERVED AGENT                                                 │
│  A small customer-support agent on a demo helpdesk.                 │
│  - Resolves tickets                                                 │
│  - Calls Gemini for reasoning                                       │
│  - Calls tools (knowledge base, refund API, ticket update)          │
│  - Emits OTEL-format traces                                         │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼  (traces)
┌─────────────────────────────────────────────────────────────────────┐
│  ARIZE                                                              │
│  - Ingests, stores, structures the traces                           │
│  - Exposes traces through its MCP server                            │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼  (read via MCP)
┌─────────────────────────────────────────────────────────────────────┐
│  THE ACCOUNTANT                                                     │
│  Built on Google Cloud Agent Builder + Gemini.                      │
│                                                                     │
│  1. Reads traces from Arize via MCP                                 │
│  2. Attaches LLM cost (tokens × model price) to each call           │
│  3. Attaches tool cost (per-call price or duration × rate)          │
│  4. Aggregates to per-trace, per-task-type, per-customer unit cost  │
│  5. Detects anomalies (e.g. cluster X costs 6× cluster Y)           │
│  6. Recommends optimizations                                        │
│  7. On approval, writes config changes to the observed agent and    │
│     re-measures                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Built with

- [Google Cloud Agent Builder](https://cloud.google.com/products/agent-builder)
- [Gemini](https://deepmind.google/technologies/gemini/)
- [Arize](https://arize.com/) (via its MCP server)
- Cloud Run for hosting both agents
- Firestore as the observed agent's config store

---

## Repository layout

```
.
├── src/
│   ├── accountant/       The Accountant agent
│   └── observed/         The demo customer-support agent
├── infra/                GCP deployment configs
├── examples/             Sample traces and Accountant outputs
├── doc/
│   ├── architecture.md   Technical overview of the two-agent design
│   ├── observed-agent.md The Helpdesk Co-Pilot: tools and instruction
│   ├── cost-model.md     How per-trace and per-task-type cost is computed
│   └── development.md    Setup, environment, and common commands
├── LICENSE
├── CLAUDE.md             Guidance for Claude Code in this repo
└── README.md
```

---

## Running it

### Prerequisites

- Google Cloud project with billing enabled
- Gemini API access (via Vertex AI or Generative Language API)
- An Arize account and API key
- `gcloud` CLI authenticated

### Environment variables

Copy `.env.example` to `.env` and fill in:

```
GCP_PROJECT_ID=
GCP_REGION=
ARIZE_API_KEY=
ARIZE_SPACE_ID=
GEMINI_API_KEY=
```

### Deploy

```bash
# Build and deploy the observed agent
cd src/observed
gcloud run deploy observed-agent --source .

# Build and deploy the Accountant
cd ../accountant
gcloud run deploy accountant --source .
```

Full setup instructions are in [`doc/development.md`](./doc/development.md). For an
overview of how the pieces fit together, start with
[`doc/architecture.md`](./doc/architecture.md).

### Try it

1. Open the observed agent's web UI and submit a few support tickets.
2. Open the Accountant's dashboard.
3. Watch the unit economics populate as traces flow through Arize.
4. Review the Accountant's recommendations and approve one.
5. Submit more tickets and watch the cost delta.

---

## Demo video

[Link to 3-minute demo on YouTube](https://www.youtube.com/) *(added before submission)*

---

## License

[MIT](./LICENSE). Free for any use, commercial or otherwise. No
warranty.

---

## Acknowledgments

Built for the Google Cloud Rapid Agent Hackathon. Thanks to the
Google Cloud, Gemini, and Arize teams for the tooling and
documentation that made this possible in three weeks.
