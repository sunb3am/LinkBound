# Phase C: scheduled outbound implementation

Status: implemented, verified locally, and deployed to Linode in no-send mode on 2026-09-23. Hosted live sends remain disabled.

## What is built

- Queue creation takes the existing preview, excludes unsendable and duplicate rows, validates LinkedIn profile URLs, and stores accepted jobs, the original source bytes, and the exclusion report in SQLite. The temporary preview cache is no longer needed after queue creation.
- `campaign_targets` persists each due time, account, job, claim, and outcome. A single in-process poller chooses one due campaign chunk at a time and calls the existing `RunCoordinator` and `LinkedInRunner`. No new browser send implementation was added.
- A local IANA timezone keeps the chosen wall-clock start time across days and daylight saving changes. Daily chunks wait for configured business hours without consuming a daily reservation. The poller selects a head per campaign so a large earlier campaign does not hide another due campaign.
- The scheduled UI is account scoped. It shows target states, due times, pause reasons, source download, and pause, resume, and cancel controls.
- The queue checks a rolling 24-hour cap and a rolling seven-day cap per sender account. Confirmed sends and unresolved possible sends consume capacity. The default weekly setting is an internal 100 action ceiling, not a LinkedIn safe limit.
- Claims are conditional and outcomes are recorded atomically with target state. On restart, an in-flight target becomes uncertain and its campaign pauses. Immediate outbound runs now write a possible-action marker before entering the browser step, so their interrupted actions also block a later send.
- The review list stays unavailable while an immediate browser action is running. Once a run finishes or is interrupted, the owner can inspect LinkedIn and record `sent` or `not sent` with a note. A target marked not sent is closed as failed and never requeued automatically. A manually confirmed send becomes an account contact fact and remains suppressed from new outreach.
- A scheduled account that needs login pauses instead of holding the browser indefinitely. Limit warnings and uncertain results also pause the campaign with a persisted reason. The working selectors and click sequence were not changed. `ProfileResult` now carries the final text for confirmed sends so the attempt log records the text actually used after name repair or AI personalization.

## Verification

- `py -m pytest -q`: 74 passed.
- `py -m compileall -q app`, `node --check static/app.js`, and `git diff --check`: passed.
- A copied local CRM database migrated from schema 0 to 5 with 623 outbound requests, 12 batches, and 459 contacts unchanged; SQLite `PRAGMA integrity_check` returned `ok`. The source database was not modified.
- A local no-send Playwright UI check created a scheduled campaign, opened target detail, paused and resumed it, downloaded its source, and reviewed a seeded uncertain immediate action. The page reported no JavaScript errors.
- Tests cover 501 due targets ahead of another campaign, business-hours deferral, same-account and cross-account duplicate suppression, exclusive claims, restart uncertainty, rolling budgets, active-run review exclusion, missing-login pause, and preservation of legacy records.

## No-send Linode deployment

Commit `cffd4d1fc9b52fcbb83667c0c8584786745df244` is active under `/opt/linkbound/current`. The deployment script verified its pre-deploy snapshot at `/var/lib/linkbound/backups/pre-deploy-cffd4d1fc9b52fcbb83667c0c8584786745df244-20260923T175154Z` and logged a successful release. `linkbound-app.service` is active. The hosted database is at schema 5 and `PRAGMA integrity_check` returned `ok`.

The persisted `profiles/me` directory still exists. The app reports `live_sends_enabled: false`, and `/etc/linkbound/app.env` retains `LINKBOUND_ALLOW_LIVE_SENDS=false`. Authorized loopback requests returned `{"campaigns":[]}` and `{"items":[]}` from the queue and review routes. An unauthenticated queue request returned HTTP 403. No campaign or live LinkedIn action was run on the host during this deployment.

## Boundaries before hosted sends

Phase C has not been exercised against a live LinkedIn action. The account owner must review the documented LinkedIn automation risk and the hosted stop behavior before any hosted send. A controlled headed send regression on an authorized account is still required because local fake-runner tests cannot prove LinkedIn UI behavior.

The existing direct-message thread heuristic can report an already present message as `message_sent`; the queue does not use that heuristic as its primary dedup rule. This remains a focused runner defect to reproduce and correct with a headed regression before treating direct-message counts as exact. The current `daily_cap` applies to all outbound action types; separate invitation, DM, and InMail budgets are deferred until needed.

Local CRM import to Linode still needs confirmation of which LinkedIn account owns `profiles/me` and how `yt` and `yash_thakkar` map. Offsite backup destination and app access on tailnet TCP 443 also remain pending from Phase B. A direct app request from the operator computer still timed out on TCP 443 after the deployment. None of those gaps enable live sends.
