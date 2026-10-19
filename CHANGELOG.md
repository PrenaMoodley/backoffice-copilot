# Changelog

## [Unreleased]

### Backlog
- Run the evaluation set live and publish the score in the README.
- Sign-in with Microsoft Entra ID; approvals record the signed-in user and check their role.
- Connect the MCP tools to PostgreSQL and the real integrations (pos-to-ledger, recon-dashboard, m365-mail-intake, marketing-spend-sync).
- Schedule the morning checks and email finance a summary.

## [0.2.0] - 2026-10-19

### Added
- Morning dashboard: occupancy, ADR, RevPAR and F&B revenue, plus six checks (room charges POS → PMS, card clearing, POS tenders, supplier invoices, bank reconciliation, ad spend), each with an "Ask copilot" button when it isn't green.
- MCP tools `hotel_day_statistics` and `pos_to_pms_room_charges` (read-only) and `post_room_charge_to_folio` (needs approval, refuses to bill twice).
- Planted problem: a pool bar check charged to room 214 that never reached the PMS.
- Demo mode: recorded answers that still run the real tools, so the app can be shared publicly with no API key. Chooses demo or live from `COPILOT_MODE` or whether a key is set.
- Live mode limits: messages per conversation and per day.
- Evaluation set (`evals/cases.json`, `python -m copilot.evals`): seven cases, including a check that every amount in an answer came from a tool. Runs in CI on recorded answers; `--live` scores the real model.
- **Reset demo** button and `POST /api/reset`.
- Plain-language summary on approval cards, with the exact tool input underneath.

### Changed
- Card clearing reconciliation counts approved journal adjustments to the clearing account, so a fix shows as balanced.
- `post_journal_adjustment` takes an optional `bank_ref` and marks that bank exception resolved.
- The app seeds fresh demo data on every start.

## [0.1.0] - 2026-10-13

### Added
- MCP server with six read-only tools, two approval-gated write tools and a chart-of-accounts resource.
- Agent loop with tool use, approval pause/resume, prompt caching, refusal fallback and an audit log.
- FastAPI app streaming every agent step over Server-Sent Events.
- Chat UI with tool-call cards, approval cards and a live audit log.
- Fictional demo data with three planted problems.
- Tests for the tools, the agent loop (approve, reject, errors, stop reasons) and the web API.
