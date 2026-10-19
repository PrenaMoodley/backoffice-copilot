# Example scope: morning finance checks for a real hotel

How I'd scope this demo as a real project. It's written for the finance manager who would
sign it off, not for developers.

## The problem

Each morning someone in finance spends an hour or more checking yesterday by hand: POS
room charges against the PMS, card takings against the acquirer's settlement, new supplier
invoices, and ad spend. Small differences like a R385 room charge or a R20 refund are easy
to miss and expensive to chase a week later.

## What changes for the team

- By 07:00 a dashboard shows yesterday's numbers and a green, amber or red light for each check.
- Anything red has the details attached: which check, which room, which bank line.
- The copilot can investigate and suggest the fix. Only someone in finance can approve it,
  and every approval is logged with their name.

## In scope (phase 1, about 4 weeks)

| Week | Deliverable |
|---|---|
| 1 | Read-only access to the PMS, POS, bank statements and accounting system agreed with IT. Data mapped for one property. |
| 2 | Checks for room charges and card clearing running nightly against real data, results compared with the manual check. |
| 3 | Dashboard live for finance. Supplier invoice and ad spend checks added. |
| 4 | Copilot with approvals switched on for two actions (post a missed room charge, put an invoice on hold). Runbook and handover. |

## Not in scope for phase 1

- Posting journals to the accounting system. The copilot drafts them; finance posts them as now.
- Other properties. Added one at a time once the first is trusted.
- Replacing any existing system or report.

## What I need

- Read-only API users for the PMS, POS and accounting system, and a CSV export of bank statements.
- One person in finance for an hour a week to check results and say what's wrong.
- A Claude API key owned by the business, with a monthly spending limit.

## Risks and how they're handled

| Risk | Handling |
|---|---|
| A check is wrong and people stop trusting it | Run it alongside the manual check for two weeks before relying on it. |
| The AI suggests a wrong fix | It can only suggest. Write actions need approval, are limited in size, and are logged. |
| Guest or card data leaves the business | Only totals, check numbers and room numbers go to the model, never names or card numbers. |
| An integration stops working | Checks show "no data" instead of "OK", and IT gets an alert. |

## How we'll know it worked

- The morning check takes under 15 minutes instead of over an hour.
- Missed room charges are found the next morning, not at month-end.
- Finance uses it every day after the first month without being reminded.
