# Phase D progress: inbound browser pilot

Phase D is in progress on `codex/linkbound-phase-d`. This branch is not deployed. The Linode still serves the Phase C release, and live sending remains disabled.

## Built in this branch

- Schema version 9 adds account-scoped sync runs, conversations, messages, attachments, positive connection observations, per-thread unread observations, durable unread open intents, and a unique LinkedIn profile URL binding for each sender. An intent is committed before an unread row is opened because LinkedIn's list row does not expose its thread URL. A later scan can retry an interrupted intent before it opens more threads.
- Ingestion requires stable thread and message keys for canonical records. Repeated observations update the same record; conflicting keys or file bytes fail instead of silently merging people. A row with uncertain identity is not opened.
- The Sessions screen saves the expected LinkedIn profile URL. Before any inbox collection or unread recovery, the headed browser reads the signed-in profile from LinkedIn's Me menu and requires an exact match. An unbound account, a mismatch, or a shared CDP context stops the scan. Message direction and contact matching use that verified URL; messages without a usable author URL remain `unknown`.
- LinkedIn unread and LinkBound reviewed remain separate. The UI shows the last pre-open unread marker and restoration result. A changed preview reopens LinkBound review.
- Private account-scoped routes and the Inbox & Sync screen list sync coverage, conversations, messages, and saved files. Bulk export includes source records, unread open intents, a contact CSV, and original saved bytes with checksum verification.
- The coordinator reserves the one headed browser for an inbound operation. The manual collector uses the existing persistent Chrome profile and visible LinkedIn inbox controls. It stops on authentication, a selector change, an unverified unread marker, or a browser failure. Its folder and row coverage are explicitly partial. There is no daily scheduler yet.
- The backup script includes saved inbound files, and the deployment migration check expects schema version 9. These changes have not been applied to the Linode production database.

## Hosted observations on 2026-09-23

- The `me` Chrome profile is Shubham Srivastava's account. The owner confirmed the signed-in feed after a browser restart.
- The visible Me menu exposed Shubham's profile URL. Its menu structure was inspected read-only to define the identity check. The new schema 9 identity check has local tests, but it has not been run as part of a hosted inbox scan.
- `https://www.linkedin.com/messaging/compose/` showed the inbox list without opening a thread. The ordinary Messaging URL auto-selected a thread. A controlled unread thread lost its unread marker when opened; the visible **Mark as unread** action restored that marker. This does not establish that a read receipt was undone.
- The list row had no `href` or other stable thread key before opening. That is why the collector now saves a visible row identity and folder as an open intent before the click. Recovery requires one exact visible match; ambiguity stops the scan.
- A bounded scan against an isolated copy of the database collected one Focused conversation. It collected one Other conversation, then Playwright reported `TargetClosedError` during its attachment step. The collector stopped. A fresh headed browser restored the Other unread marker; the copy recorded the intent and thread observation as `restored`. No attachment was saved. This failure has occurred twice in the integrated pilot, while a separate direct download of the same small PDF succeeded once. The cause is not established.
- Tailscale access to the private app now works. `https://linkbound-01.tailfbed29.ts.net/api/v1/health` returned HTTP 200 and `{"ok":true,"version":"2.0.0","busy":false}` after the pilot. The app service restarted and remains on Phase C.

## Verification and release gate

`py -m pytest tests -q` passed with 104 tests. `py -m compileall -q app scripts`, `node --check static/app.js`, and `git diff --check` passed. The targeted collector tests cover repeat scans without duplicate records, unread restoration after an attachment failure, stopping on an unverified marker, recovery when Chrome closes before returning the thread URL, and account identity mismatches. The hosted copied database migrated to schema 8 and recorded the pilot without changing the live database. The temporary copied data and pilot probe files were removed after inspection. Schema 9 and its UI have local checks, but no hosted inbox run yet.

Phase D is not ready for a daily job or production rollout. Next work:

1. Diagnose the integrated `TargetClosedError`, then prove file capture and export with actual bytes in a bounded hosted scan. Preserve the restored unread marker and stop conditions while doing this.
2. Bind each sender's LinkedIn profile URL in Sessions and verify the schema 9 identity check on the hosted headed browser. Add safe coverage of remaining rendered conversations and message requests, then tracked invitation and positive connection evidence. Make historical and incremental coverage, source timestamps, and uncertain identities explicit.
3. Add the serialized daily schedule only after browser collection is stable. Run two controlled scans to verify no duplicate messages or files and that unread restoration survives restart.
4. Verify backup and restore for saved files, add an encrypted offsite copy, and deploy Phase D with a migration and rollback check. Keep hosted sends disabled until the separate account-owner risk decision and controlled send gate in the access risk review.

LinkedIn operations remain browser-only. The app's own private API is for its UI and CRM exports; it is not a LinkedIn API integration.
