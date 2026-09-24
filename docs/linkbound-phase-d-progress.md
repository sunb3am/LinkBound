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

## Hosted observations on 2026-09-23 and 2026-09-24

- The `me` Chrome profile is Shubham Srivastava's account. The owner confirmed the signed-in feed after a browser restart.
- The visible Me menu exposed Shubham's profile URL. A schema 9 scan on an isolated database copy verified that URL before opening the inbox. LinkedIn's visible account menu did not consistently appear through Playwright's accessibility role and text filters. The collector now checks the rendered menu directly, and the hosted scan passed that gate.
- `https://www.linkedin.com/messaging/compose/` showed the inbox list without opening a thread. The ordinary Messaging URL auto-selected a thread. A controlled unread thread lost its unread marker when opened; the visible **Mark as unread** action restored that marker. This does not establish that a read receipt was undone.
- The list row had no `href` or other stable thread key before opening. That is why the collector now saves a visible row identity and folder as an open intent before the click. Recovery requires one exact visible match; ambiguity stops the scan.
- A bounded schema 9 scan against an isolated copy of the database collected one Focused conversation and one Other conversation. The Other PDF download event fired, then Chrome exited with `SIGSEGV` while Playwright awaited `download.path()`. The collector stopped. A fresh headed browser restored the Other unread marker; the copied database recorded its open intent and thread observation as `restored`. No attachment was saved. An earlier direct download of the same small PDF succeeded once, so the trigger for Chrome's crash remains unknown. The hosted browser was Chrome 154.0.8037.57 with Playwright 1.63.0. No kernel out-of-memory event was found. A Chrome minidump with the crash timestamp remains on the host under the service account's Chrome Crash Reports directory.
- Tailscale access to the private app, browser viewer on port 8443, and SSH on port 22 works. After the diagnostic, `https://linkbound-01.tailfbed29.ts.net/api/v1/health` returned HTTP 200 and `{"ok":true,"version":"2.0.0","busy":false}`. The app service is active and still serves Phase C.

## Verification and release gate

`py -m pytest tests -q` passed with 105 tests. `py -m compileall -q app scripts`, `node --check static/app.js`, and `git diff --check` passed. The targeted collector tests cover repeat scans without duplicate records, unread restoration after an attachment failure, stopping on an unverified marker, recovery when Chrome closes before returning the thread URL, and account identity mismatches. The hosted copied database migrated to schema 9 and recorded the bounded scan without changing the live database. The schema 9 identity check passed in the headed browser. The temporary diagnostic data, archive, and probe files were removed after inspection; the Chrome minidump was retained for diagnosis.

Phase D is not ready for a daily job or production rollout. Next work:

1. Investigate why hosted Chrome exits with `SIGSEGV` after this PDF download event. Then prove file capture and export with actual bytes in a bounded hosted scan. Preserve the restored unread marker and stop conditions while doing this.
2. Bind each sender's LinkedIn profile URL in Sessions after deploying schema 9. Add safe coverage of remaining rendered conversations and message requests, then tracked invitation and positive connection evidence. Make historical and incremental coverage, source timestamps, and uncertain identities explicit.
3. Add the serialized daily schedule only after browser collection is stable. Run two controlled scans to verify no duplicate messages or files and that unread restoration survives restart.
4. Verify backup and restore for saved files, add an encrypted offsite copy, and deploy Phase D with a migration and rollback check. Keep hosted sends disabled until the separate account-owner risk decision and controlled send gate in the access risk review.

LinkedIn operations remain browser-only. The app's own private API is for its UI and CRM exports; it is not a LinkedIn API integration.
