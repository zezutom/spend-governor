# CLAUDE.md

Guidance for Claude (and Claude Code) when working in this repository.

## What this repo is

The source code for an agent that produces unit economics for AI
agents — built as a submission to the
[Google Cloud Rapid Agent Hackathon](https://rapid-agent.devpost.com/).

The agent reads traces from Arize (via its MCP server), computes
per-trace and aggregated unit cost, detects cost anomalies, and on
approval executes optimization changes against the observed agent's
configuration.

## Hard constraints (from the hackathon rules)

These are non-negotiable. Any generated code must respect them.

- **Stack:** Google Cloud Agent Builder + Gemini + Arize MCP server.
  No competing cloud AI platforms. No AI tools outside Google Cloud
  AI services and Arize's built-in AI features.
- **Target platform:** web.
- **Open source.** MIT licensed. Do not add code, data, or assets
  that cannot be released under MIT.
- **No third-party advertising, logos, or sponsorship indicators**
  in any artifact intended for the demo video. Google Cloud and
  Arize branding is fine; other vendor logos are not.
- **Project must function as depicted.** No mocked behavior dressed
  up as real. If the demo shows X happening, X must actually happen.

## Architecture in one paragraph

The system has two pieces. **The observed agent** is a small
customer-support agent that resolves tickets on a demo helpdesk; it
calls Gemini for reasoning and a small set of tools (knowledge base
lookup, refund API, ticket update), and emits OTEL-format traces to
Arize. **The Accountant** is the product — built on Google Cloud
Agent Builder + Gemini — which reads the observed agent's traces
through Arize's MCP server, attaches LLM and tool costs, aggregates
to per-trace and per-task-type unit economics, detects anomalies,
generates optimization recommendations using Gemini, and on operator
approval writes config changes back to the observed agent's config
store. The observed agent picks up the new config on its next run,
and the Accountant re-measures to show the cost delta.

## Things to do before writing code

If asked to generate substantial implementation code, check that:

1. The thin-slice scope for the current week is recorded somewhere
   in the repo.
2. The observed agent's trace shape is defined (what fields, what
   tool-call metadata, what customer attribution).
3. The cost model is defined (LLM token pricing source, tool
   pricing defaults).

If those aren't recorded, propose them in writing first.

## Style and approach for code

- **Optimize for the demo, not for production.** Within ethical and
  rule-compliant limits, the goal is a working, demo-legible system
  on a 21-day timeline. Defer multi-tenancy, hardening, retries, and
  generalization unless they're needed for the submission.
- **Prefer GCP-native services** where they fit (Agent Builder,
  Gemini API, Cloud Storage, Cloud Run, Firestore, BigQuery).
- **Make the Arize MCP integration visibly load-bearing.** The
  Stage 1 viability check explicitly looks for meaningful partner
  application. The MCP server's role should be obvious in the code,
  the architecture, and the video.
- **Keep the agent loop visible.** The hackathon rule is to "move
  beyond chat." The Accountant must plan, use tools (Arize MCP +
  config-write), and execute — and the demo must show this, not
  abstract it away behind a chat UI.
- **The cost-attribution math must be auditable.** Every per-trace
  cost number should be traceable to its components (which LLM
  call, which token count, which tool call, which unit price). No
  magic numbers in the dashboard.

## Style and approach for the demo

When asked to help with demo content:

- **3 minutes hard cap.** Only the first 3 minutes are evaluated.
- **English (or English subtitles).** No exceptions.
- **Show real functionality.** No "imagine if…" hand-waving. The
  observed agent must actually run; the Accountant must actually
  read real traces; the optimization must actually change behavior
  and produce a measurable before/after delta.
- **The first 30 seconds is critical.** The premise (an agent
  watching another agent) is meta-recursive and easy to misread.
  Frame it cleanly upfront or judges get lost.

## Repository conventions

- `src/accountant/` — the Accountant agent code.
- `src/observed/` — the demo support agent the Accountant monitors.
- `infra/` — GCP deployment configs (Terraform / gcloud scripts /
  Cloud Run configs).
- `examples/` — sample traces, sample config files, sample
  Accountant outputs.
- `docs/` — public-facing documentation, including architecture
  overview.
- `LICENSE` — MIT.
- `README.md` — what the project is, how to run it, link to demo
  video.

## What not to do

- **Do not commit credentials, API keys, or service account JSON.**
  Use environment variables and document them in the README.
- **Do not commit customer or trace data that isn't synthetic.** All
  example traces in this repo are generated for the demo. If real
  user data ever appears here, it's a bug to fix immediately.
- **Do not add dependencies that aren't OSI-permissively-licensed.**
  GPL/AGPL/SSPL/BSL dependencies break the submission's licensing
  requirement.
- **Do not reach for non-Google AI tools.** No OpenAI, Anthropic,
  Mistral, etc. The rules permit only Google Cloud AI services and
  Arize's built-in AI features.

## Tone

Speak at peer level. The maintainer is a senior engineer. Skip
restating problems back before answering; skip excessive
disclaimers. When pushing back on a request, be direct about why
and propose an alternative.

## When in doubt

Ask. A clarifying question is cheaper than the wrong 500 lines of
output.
