# backoffice-copilot

Morning finance dashboard for a fictional hotel, with a Claude agent. MCP server over
back-office data (`mcp_server.py`), dashboard checks built on the same functions
(`checks.py`), agent loop with human approval (`agent.py`), FastAPI + SSE web app
(`web.py`, `static/`), recorded answers for demo mode (`demo.py`), evaluation set
(`evals.py`, `evals/cases.json`).

## Commands

- Install: `pip install -e ".[dev]"`
- Test: `python -m pytest -q` (no API key; Claude is faked or recorded)
- Evals: `python -m copilot.evals` (recorded), `python -m copilot.evals --live` (needs `ANTHROPIC_API_KEY`)
- Lint: `python -m ruff check . && python -m ruff format --check .`
- Run: `uvicorn copilot.web:app` (demo mode unless `ANTHROPIC_API_KEY` is set)
- Container: `docker build -t backoffice-copilot . && docker run -p 8000:8000 backoffice-copilot`, then `curl localhost:8000/health`

## Rules

- Every figure comes from a tool, calculated with `Decimal` in `mcp_server.py`.
  Never ask the model to add up or recalculate money.
- The dashboard (`checks.py`) calls the MCP server's functions. Never calculate a figure
  for the dashboard anywhere else, or the dashboard and the copilot will disagree.
- A new tool that only reads must be annotated `READ_ONLY`. Anything else is
  `WRITES` and will need approval. Never mark a writing tool read-only to skip approval.
- Write tools must refuse to do the same thing twice (post a check twice, resolve a bank
  line twice) with a `ToolError` that says why.
- Validation errors in tools raise `ToolError` with a message Claude can act on.
- The agent's history is append-only: keep Claude's content blocks exactly as returned.
- All tool results for one assistant turn go back in one user message, in call order.
- Tool definitions stay sorted and the system prompt stays static (apart from the
  date), so prompt caching keeps working.
- Recorded answers in `demo.py` may only quote amounts the script's own tool calls return.
  `python -m copilot.evals` checks this; run it after changing a script or the data.
- Every question a dashboard check asks (`checks.QUESTIONS`) needs a recorded script.
- Run `python -m copilot.evals --live` before changing the system prompt, a tool
  description or the model, and add a case to `evals/cases.json` for every new planted problem.
- Demo data in `data.py` is fictional. Keep the planted problems; the tests and evals rely on them.
- Don't name real hotels, suppliers or guests anywhere in the repo.
- `/health` stays read-only and fast, and reports what failed. If you add a dependency the
  app can't work without, add it to the health check.
- Update `docs/RUNBOOK.md` when you add a failure mode, a setting or an environment variable.
  Secrets go in the host's secret store, never in the image, the repo or a log line.
