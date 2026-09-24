# Phase D progress: deployed inbound browser collector

Status on 2026-09-23 Pacific: `codex/linkbound-phase-d` is pushed and deployed
on the private Linode at code commit `048179e`. Shubham's `me` profile is the
only bound LinkedIn account. Hosted live sending remains disabled. The daily
no-send inbound schedule is enabled for 18:00 America/Los_Angeles.
The app is available at `https://linkbound-01.tailfbed29.ts.net/`; the headed
browser viewer is at `https://linkbound-01.tailfbed29.ts.net:8443/`. Both use
Tailscale Serve. The tailnet policy still needs the broader 443/8443 grant in
[the runbook](linkbound-phase-b-runbook.md) before every member device can use
them.

## What is deployed

- Schema 9 stores account-scoped sync runs, conversations, messages, files,
  positive connection observations, and durable unread open intents. Each
  sender has a unique expected LinkedIn profile URL. The browser checks the
  signed-in Me menu against that binding before collecting anything.
- The existing headed, persistent Chrome profile and shared run coordinator
  perform browser-only inbox collection. The collector inspects Focused,
  Other, Archived, and Spam, prioritizes changed unread rows, and records
  partial coverage. Message Requests and Sent Invitations are explicitly
  `not_scanned` because Shubham's rendered inbox exposed no Requests control
  and the Sent Invitations collector is not built.
- Opening an unread conversation changes its LinkedIn unread marker. The
  collector commits an open intent first, restores the visible marker after
  collection, and stops if restoration cannot be verified. This cannot undo
  a possible read receipt. LinkBound's reviewed flag is independent.
- The Inbox & Sync UI and private account-scoped routes show sync coverage,
  conversations, message text, files, and a ZIP export with source records and
  checksums. The app API serves LinkBound data; LinkedIn access remains entirely
  through the headed browser.
- A daily no-send poller runs in the single app process. It is off by default
  in source configuration and enabled on this host for Shubham. It opens at
  most 2 changed conversations per folder and inspects at most the first 20
  list rows per folder. It records
  everything beyond that limit as incomplete. A manual scan may inspect up to
  500 list rows and open up to 20 changed conversations per folder.
- Previously invited account contacts are checked in a rotation of at most 5
  profiles per run. Only a verified first-degree profile records acceptance.
  Shubham currently has no tracked invited contacts in the hosted database, so
  this path has unit proof but no live account proof.

## Hosted evidence

- The first schema 9 scan on an isolated database copy proved the signed-in
  profile binding and the unread restoration path. A real attachment click
  triggered a Chrome 154 `SIGSEGV` after a persistent-profile restart. The
  failure also reproduced with local test files and a disposable Chrome
  profile, without LinkedIn. The root cause inside Chrome is not known.
- Attachment capture now reads the completed response bytes through Chrome
  DevTools Fetch during a visible download-button click and denies the native
  download manager for that click. It does not call a LinkedIn API. Offline
  2 MiB PDF transfers succeeded across repeated browser restarts. The hosted
  no-send run 4 then captured one real file without a crash, restored three
  unread markers, and left one already-read thread read. The downloaded file
  had 52248 bytes; its SHA-256 matched stored metadata and the ZIP export.
- Hosted run 5 completed without a crash, with 4 conversations, 5 messages,
  and 1 file still recorded. Runs 4 and 5 had partial folder coverage by
  design. The collector's repeat tests verify message and file deduplication;
  the hosted pre-run message count was not separately recorded.
- Hosted inventory run 6 completed with `stopped=false`, but its coverage is
  partial: Focused observed 500 rows and hit the inspection cap; Other
  observed 16; Archived 203; Spam 4. One changed conversation was stored from
  each folder. Requests and Sent Invitations remained `not_scanned`. This
  exposed a large backlog; a daily 500-row traversal would be too broad for
  the initial scheduled job.
- Hosted run 7 used the same 20-row list cap as the daily schedule and exited
  with `stopped=false`. It observed 20 Focused, 16 Other, 20 Archived, and 4
  Spam rows, opening 2 changed conversations from each folder. The database
  recorded 1 restored unread marker and 7 already-read conversations. The CRM
  then held 16 conversations, 16 messages, and 1 file. Focused and Archived
  explicitly reported that their lists extended beyond the cap.
- After run 7, the private app returned
  `{"ok":true,"version":"2.0.0","busy":false}`. Its configuration reported
  `inbound_schedule.enabled=true`, operator `me`, time `18:00`, timezone
  `America/Los_Angeles`, 2 opened rows per folder, and
  `live_sends_enabled=false`. A local snapshot including saved files passed
  verification. An isolated restore copy passed the same manifest verification. No encrypted
  offsite destination is configured yet.
- The restic offsite job and nightly timer are staged in this branch, but the
  timer is not installed or enabled. A temporary local restic repository on the
  Linode proved encrypted backup, `restic check`, full restore, and LinkBound
  manifest verification. The private Object Storage bucket and limited key are
  still needed for an actual offsite upload and restore drill.
- The current release's root-owned files were normalized to remove group write
  access, and the deployment script now enforces that permission on new
  releases. The app health check remained successful after the change.

## Remaining before closing Phase D

1. Observe the first automatic daily run and confirm the same bounded
   coverage, unread restoration, and no duplicate messages or files. Keep
   incomplete coverage prominent in the UI. Add deliberate backfill in small
   manual batches before claiming complete history.
2. Observe a real Message Requests control if one appears, then add a tested
   browser collector. Build Sent Invitations observation without treating a
   disappeared invitation as accepted. Verify first-degree acceptance against
   an actual tracked Shubham contact. Older message history and source time
   require visible browser evidence rather than inferred timestamps.
3. Configure the private Object Storage destination and limited key, run an
   actual encrypted upload and restore drill, then enable the nightly timer.
   The local database and attachment restore is proven; offsite resilience is
   not.
4. Run a controlled hosted send against a specified target and exact text to
   verify the outbound stop controls. The user has authorized internal use of
   Shubham's account; no separate account-owner approval gate is needed.
   Live sends remain disabled until that concrete regression is complete.

The current risk controls and the successful no-send pilots do not establish
that LinkedIn will allow or fail to detect the automation. See the
[access risk review](linkbound-linkedin-access-risk-review.md).
