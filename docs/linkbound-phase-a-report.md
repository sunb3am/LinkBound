# Release A implementation record

Status: implemented on `codex/linkbound-phase-a`, awaiting deployment and a controlled headed regression run. No LinkedIn sends were made for this release.

## What changed

- Account relationships now live in `account_contacts`, keyed by sender account and normalized LinkedIn URL. The old global `contacts` row supplies shared profile details, not outbound status. Migration version 2 backfills attributed request history and URL keys, including legacy `www.linkedin.com` forms, without rewriting the original URL or attempt rows. Contact-only legacy status is marked `legacy_unverified` and suppresses another send on that sender account without claiming a confirmed send. Ambiguous old ownership is not assigned across conflicting senders.
- A confirmed send remains an immutable `outbound_requests` fact. Preview and the live pre-send gate use the same dedup rule: global suppression after a confirmed send, plus positive pending/connected observations for the selected account. The global rule is the current conservative default. There is a policy hook for a future reviewed cross-account override, but no UI override yet.
- Recording an outcome now writes the request, account observation, and shared profile details in one SQLite transaction. Later skips and failures cannot clear prior successful send evidence or positive relationship state. Empty captured profile fields do not erase known details.
- Dashboard starts, internal API starts, and profile name resolution use one in-process coordinator. It admits one browser operation at a time across sender accounts. Batch IDs are allocated before the start response. The existing Playwright send selectors and click flow were not modified.
- An account with campaign or outreach history or a populated browser profile cannot be deleted and recreated under the same key, which would silently attach old history or a login to a new identity. Active accounts also cannot be deleted during a browser operation.
- Contacts, batch history, analytics, and the contact timeline are filtered by sender account. The live run panel remains global so pause and stop stay reachable if the account picker changes during a run. API callers of these account-scoped reads must now supply `operator`.
- The old scheduler that only relabeled due campaigns as running has been removed. Durable scheduling and crash reconciliation belong to Release C.

## Verification

- `py -m pytest -q`: 15 passed, using temporary SQLite databases, a mocked start coordinator, and a fake browser runner. Tests cover two accounts, legacy URL reconciliation, migration rollback, send retention after a skip, failed-only contact visibility, account-scoped API reads, the shared start path, a second target being skipped before it reaches the browser after the first target sends, account identity protection, and a missing campaign mutation leaving no open transaction.
- `py -m compileall -q app`, `node --check static/app.js`, and `git diff --check` passed.
- No browser or LinkedIn session was opened by these checks. A controlled headed dry run on the existing local setup remains the deployment gate for this code change.

## Limits and next gate

An attempt that may have clicked Send immediately before a process crash still lacks a durable uncertain state. Release C adds pre-action target leases, uncertainty, and reconciliation before retry. The prepared `message_rendered` field is not proof of the final text actually sent; the queue release must capture that separately. The API/UI authorization boundary, persistent service installation, and backup/restore test are Release B. The risk review's challenge and limit stop checks plus account-owner acceptance remain prerequisites to hosted sends.
