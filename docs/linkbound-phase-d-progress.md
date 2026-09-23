# Phase D progress: inbound CRM foundation

Phase D is in progress on `codex/linkbound-phase-d`. This branch has not been deployed. The Linode app remains on the Phase C release with live sending disabled.

## Implemented in this branch

- Schema version 6 adds account-scoped sync runs, conversations, messages, attachments, and positive relationship observations. A restarted in-progress scan becomes `interrupted`.
- Ingestion requires stable thread and message keys. A row without a stable thread key counts as unresolved section coverage. Repeated observations update the same record; conflicting keys or file bytes raise an error instead of silently merging people.
- LinkedIn unread and LinkBound reviewed are separate fields. A changed conversation preview reopens LinkBound review.
- Positive first-degree evidence can combine with a confirmed invitation to show an accepted milestone. Replied and file-received milestones require an inbound message and saved file on a linked conversation.
- Private account-scoped routes list sync health, conversations, messages, and file downloads. The Inbox & Sync screen shows partial coverage, unmatched and ambiguous contacts, unread/review status, message details, and files. Bulk export includes a contact CSV, observation records, and original saved bytes, with archive checksums verified.
- The shared coordinator now reserves the browser for a no-send inbound operation and prevents an outbound run or name-resolution pass from starting alongside it.
- A focused adversarial review led to four corrections: required section coverage is declared when a run starts, CSV cells are inert in spreadsheets, conflicting attachment bytes leave no new stored file, and unread values accept only a Boolean or unknown.

## Hosted browser observation

On 2026-09-23, with `linkbound-app` stopped to give the Chrome profile exclusive ownership, a no-send Playwright probe opened `https://www.linkedin.com/messaging/`. LinkedIn redirected to a conversation thread automatically while displaying the conversation list. We closed Chrome, removed the probe files, and restarted `linkbound-app`; `systemctl is-active linkbound-app` returned `active`.

There was no recorded unread baseline for the auto-selected thread, so this probe does **not** establish whether it changed unread status or sent a read receipt. Do not run a daily thread-opening collector from this evidence alone. LinkedIn Help says unread badges persist until a conversation is opened, and its delivery indicators can show read receipts when enabled: [message indicators](https://www.linkedin.com/help/linkedin/answer/a569649), [mark read or unread](https://www.linkedin.com/help/linkedin/answer/a540960/mark-a-conversation-as-read-or-unread?lang=en), [delivery indicators](https://www.linkedin.com/help/linkedin/answer/a567370).

## Verification

- `py -m pytest tests -q`: 90 passed.
- `py -m compileall -q app`: passed.
- `node --check static/app.js`: passed.
- Local Playwright UI check with synthetic conversations: two rows, partial sync coverage, message/file detail, and no page script errors. The local test server was stopped afterward.
- A temporary copy of the local CRM database migrated to schema 6 with SQLite integrity `ok`; its 623 outbound requests, 12 batches, and 459 contacts remained present. The original database was not modified, and the temporary copy was removed.

## Remaining before Phase D is usable

1. Confirm whose LinkedIn login lives in the hosted `me` profile before associating observations with that account.
2. Decide whether the daily collector may open unread threads. Record a before/after unread-state observation on the hosted browser.
3. Inspect actual inbox row and thread structure, then implement bounded browser-only collection across relevant sections, messages, invitations, and file downloads. Stop on login, challenge, restriction, or uncertain selector state.
4. Add the scheduled daily run and make coverage explicit for every expected section. Complete two controlled scans to prove no duplicate messages or files.
5. Verify export against real captured file bytes and deploy only after the browser behavior and account mapping are confirmed. Keep live sending disabled.
