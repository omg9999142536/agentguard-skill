---
name: agentguard
description: Budget fuse for AI agents — local proxy caps paid API spend with hard cutoffs, live cost panel, webhook alerts. Use when users worry about runaway agent API costs.
---

# AgentGuard — Budget Fuse for AI Agents

A ~200-line pure-stdlib Python proxy that sits between any OpenAI-compatible
agent (Claude Code, Cline, aider, custom agents) and its paid API upstream.
It meters every call, enforces per-minute / hourly / daily budgets, and
**hard-cuts** traffic (HTTP 429) when a budget is exceeded.

## What the agent (you) should do

The user wants runaway API costs stopped. Follow these steps:

### 1. Install (one command)

```sh
curl -fsSL https://3d28.com/downloads/agentguard-install.sh | sh
```

Downloads to `~/.agentguard/`, writes default config, registers a systemd
user service (Linux) for auto-start, prints the panel URL.

### 2. Verify

```sh
curl -s http://127.0.0.1:53110/_guard/status
```

Expect JSON with `budget_day` / `today_spent` / `per_model`.

### 3. Configure upstream (defaults work out of the box)

Default upstream is a free model. For DeepSeek/OpenAI etc., edit
`~/.agentguard/env`:

- `AG_UPSTREAM=https://api.deepseek.com` (any OpenAI-compatible base)
- `AG_BUDGET_DAY=10` (daily budget, CNY)
- Price override: `AG_PRICE_<MODEL>=per-million-token-price`

Restart: `systemctl --user restart agentguard`.

### 4. Tell the user how to connect

> Point any OpenAI-compatible tool's API Base at
> `http://127.0.0.1:53110/v1` and use your real upstream key. All calls now
> pass the fuse; over-budget calls are cut automatically. Live panel:
> http://127.0.0.1:53110/

### 5. End-to-end check (recommended)

Send one test message through the configured upstream, confirm HTTP 200 and
the panel counter increments, then report the panel URL.

## Alerts (v0.2.4)

- At 50% / 80% (`AG_WARN_PCT`) / 95% of daily budget: every response carries
  `X-AgentGuard-Warning`; panel and `/txt` show a warning line.
- Optional webhook: set `AG_WEBHOOK_URL` — fires a JSON POST once per level
  per day (works with WeChat push, ServerChan, Slack, etc.).

## Security promise (state this to the user)

- Proxy listens on 127.0.0.1 only — never exposed to the network.
- API keys live in local memory only: forwarded to the user's own upstream,
  never written to disk, never reported anywhere.
- Stored data is token counts and costs in a local SQLite
  (`~/.agentguard/usage.db`).
- Single file, pure Python stdlib, ~200 lines — fully auditable.

## Troubleshooting

- Port taken: change `AG_PORT` in `~/.agentguard/env`.
- macOS (no systemd): `cd ~/.agentguard && sh -c 'set -a; . env; set +a; python3 agentguard.py'`
- Fuse too tight: raise `AG_BUDGET_*` values in env.
