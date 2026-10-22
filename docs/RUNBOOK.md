# Runbook: backoffice-copilot

## What it does

Morning finance dashboard for a (fictional) hotel, plus a Claude copilot that
investigates failed checks and proposes fixes that a person must approve. One
container: FastAPI web app, which starts the MCP server as a child process and talks
to it over stdio. Data: SQLite file in `COPILOT_DATA_DIR`, re-seeded with the demo
data on every start.

## Health check

```bash
curl -s https://<host>/health
# {"status":"ok","mode":"demo","problems":[]}          <- healthy (HTTP 200)
# {"status":"unavailable",...,"problems":["..."]}      <- unhealthy (HTTP 503), says what failed
```

The container also has a Docker `HEALTHCHECK` on the same endpoint, so `docker ps`
shows `healthy` / `unhealthy`, and hosts can restart it automatically. CI builds the
image, starts it and waits for it to report healthy on every push.

## Settings

| Variable | Default | Notes |
|---|---|---|
| `COPILOT_MODE` | `live` if an API key is set, else `demo` | Force `demo` for a public link |
| `ANTHROPIC_API_KEY` | none | Secret. Live mode only. From the host's secret store, never the image or repo |
| `COPILOT_MESSAGES_PER_SESSION` | `12` | Live mode cap per conversation |
| `COPILOT_MESSAGES_PER_DAY` | `300` | Live mode cap for the whole app |
| `COPILOT_DATA_DIR` | `/home/copilot` in the image | Database and audit log |
| `COPILOT_MODEL` | see `agent.py` | Change only after running `python -m copilot.evals --live` |
| `PORT` | `8000` | Set by hosts such as Render |

## Common failures

### 1. `/health` returns 503 "MCP server not responding"
The child process that serves the tools has died or hung.
1. Check the container logs for a Python traceback from `copilot.mcp_server`.
2. Restart the container. The MCP server starts with the app.
3. If it keeps happening, the traceback says which tool crashed. Fix it and add a test.

### 2. `/health` returns 503 "database unreadable"
1. Check that `COPILOT_DATA_DIR` exists and the `copilot` user can write to it.
2. Restart: the app re-creates the demo database on start-up.

### 3. The copilot says "Could not reach the Claude API" or "Claude API error 429/529"
1. Check status.anthropic.com.
2. `401`: the API key is wrong or revoked. Replace it in the host's secrets and restart.
3. `429`: rate limited. The SDK already retries; wait, or lower the daily cap.
4. While it's down, the dashboard still works: the checks don't use the API.

### 4. Users see "today's usage limit" in live mode
Working as designed: `COPILOT_MESSAGES_PER_DAY` was reached. Raise it only after
checking the spend in the Claude Console. Resets at midnight (server time).

### 5. Answers appear all at once instead of step by step
A proxy in front of the app is buffering the stream. The app sends
`X-Accel-Buffering: no`; also turn off response buffering for `/api/chat` and
`/api/decision` in the proxy, and allow long-lived responses (60s+).

### 6. Demo data is in a strange state
Someone approved or rejected fixes. Click **Reset demo** (or `POST /api/reset`), or restart.

## Deploy and roll back

1. Push to `main`. CI must pass: lint, tests, evaluation set, and the Docker build and smoke test.
2. The host builds and runs the `Dockerfile` (Render, Fly.io or a VPS).
3. Check `/health`, then load the dashboard.
4. Roll back by redeploying the previous commit or image on the host. The demo data
   is re-seeded on start, so there's no data migration to undo.

## Scaling limits (read before running more than one instance)

- Conversations are held in memory: run **one instance**, or move sessions to
  PostgreSQL/Redis first. A second instance won't know about the first one's sessions.
- SQLite is a local file per container. Production would use managed PostgreSQL
  with backups, and a tested restore.

## Secrets

| Name | Where | Rotate |
|---|---|---|
| `ANTHROPIC_API_KEY` | Host secret store | Create a new key in the Claude Console, update the host, restart, then delete the old key |

## Dependencies

Dependabot opens weekly update PRs (Python packages, GitHub Actions, the Docker base
image). Merge only when CI is green.
