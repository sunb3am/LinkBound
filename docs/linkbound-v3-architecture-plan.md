# LinkBound v3 architecture and delivery plan

Status: proposed for founder review, 2026-09-22.

## Outcome

LinkBound becomes a continuously available internal outbound operations system. Shubham can switch among distinct LinkedIn accounts, queue large campaigns, see accepted requests and replies, inspect and export shared files, and promote qualified candidates into Cruitical. Each account retains its own browser login, limits, contact history, and sync health. The existing browser send path remains the reference implementation until a specific defect is reproduced and fixed.

## Evidence and constraints

- The current app has a FastAPI dashboard, a Playwright persistent Chrome context per operator, SQLite contact and batch records, and an MCP and HTTP client. The local database contains completed outbound batches.
- The `contacts` table has one row per URL and one mutable `operator` and `last_status`. This loses account context and can erase dedup evidence. Previewed dedup is not checked again before a send.
- Dashboard runs use per-operator orchestrators. The `/api/v1/enqueue` route uses another global orchestrator. Its batch ID is assigned asynchronously after the route can return.
- Campaign scheduling currently marks a due campaign `running` without target rows or sends. Upload previews live only in process memory.
- The existing dashboard routes, WebSocket, export, and file route have no authentication. The API key protects only `/api/v1/*`.
- The current Dockerfile installs Chromium, while configuration requests headed Google Chrome. Its Playwright image and Python package versions are not pinned together.
- The session picker changes the visible account name, but the current CRM query remains global and limited. The Safety Limits Save control does not persist the entered values.
- LinkedIn says third-party tools that automate activity on its website violate its rules. A headed browser and the user's report of no account notices so far do not establish future non-detection. LinkedIn does not publish 100 daily invitations as a safe allowance. The user-defined cap is an internal ceiling, not a platform guarantee.
- Cruitical's current admin candidate creation and resume upload routes require a human admin credential. Admin creation makes a claimable user, resume upload can overwrite an existing file, and passport regeneration can interrupt an existing run. They are not a safe automatic import contract yet.

## Deployment choices

| Choice | Benefit | Cost or risk | Recommendation |
| --- | --- | --- | --- |
| All components on one Linode VM immediately | Simple operations and always-on browser | New IP and Linux browser environment, so account behavior may change | Pilot only after the control plane is stable |
| Linode control plane plus current local browser worker | Always-on CRM and queue while keeping the known browser setup | Sends and sync wait while the local worker is offline | First production release |
| Approved LinkedIn API integration | Stable provider contract if access is granted | General invitations and communications access is restricted to approved partners; it does not meet the near-term schedule | Explore separately, do not block the first release |

The same worker protocol runs locally or on the VM. Deployment location is configuration, so changing it does not change send decisions, dedup, audit, or scheduling. The first Linode browser pilot checks login and read-only navigation with one account. Autonomous sends move only after a measured pilot and explicit account owner acceptance of the platform risk. If the pilot fails, the local worker remains the sending path.

```mermaid
flowchart LR
  UI[Private web UI] --> API[FastAPI control plane]
  Ext[Cruitical integration] --> API
  API --> DB[(SQLite on persistent disk)]
  API --> Files[(Managed file storage)]
  Worker[Browser worker, local or Linode] <-->|Authenticated claim and result API| API
  Worker --> P1[Chrome profile: Shubham]
  Worker --> P2[Chrome profile: Aarushi]
  Worker --> P3[Future employee profiles]
```

One API process owns database writes and runs with one Uvicorn worker. A worker never opens the server's SQLite file over a network share. Start with one browser worker and one active browser context at a time. If volume later justifies more workers, database leases and account locks remain the coordination mechanism.

## Data contracts

1. `accounts`: one LinkedIn sender identity, owner label, timezone, profile location, login state, last sync, pause state, and per-account policy. An account is not an app user. Shubham is the initial app user and can manage several accounts.
2. `people` and `profile_aliases`: a canonical LinkedIn person identity and every observed URL variant. Normalize `www.linkedin.com` and `linkedin.com`, query strings, case, and trailing slashes. An alias collision is reviewed, never silently merged.
3. `account_contacts`: one relationship per `(account_id, person_id)`. Store derived current state and review state here; keep source observations in append-only `activity_events`. A separate global suppression rule can prevent another account from contacting the same person without erasing either account's history.
4. `campaigns` and `campaign_targets`: durable audience rows with account, position, action, template version, rendered preview, due time, state, attempt number, and an immutable outbound result. Uploading 500 rows creates durable draft targets. Starting or scheduling never depends on `_UPLOADS` memory.
5. `conversations`, `messages`, and `attachments`: account-scoped provider observations. Preserve timestamps, direction, sender, body, source link, observed time, and a provider ID when exposed. Otherwise use a stable fingerprint and flag weak matches for review. Store attachment name, type, size, SHA-256, download state, source message, and storage path.
6. `sync_runs` and `integration_deliveries`: per-account sync cursor, outcome, errors, and source coverage; separate delivery attempts and idempotency keys for Cruitical promotion.

Statuses are independent facts: `invited`, `accepted`, `replied`, `file_received`, `reviewed`, and `promoted_to_cruitical` can coexist. An absent pending invitation is not proof of acceptance. Show the last observed time and source for every inferred status. A failed or skipped attempt cannot remove a prior send.

Migration first makes a backup and creates versioned schema migrations. Reconstruct account-specific historical events from `outbound_requests.operator`. Preserve the old `contacts` row as legacy evidence when ownership or status cannot be reconstructed. Do not invent acceptance, replies, or account ownership from old rows. Compare legacy and migrated counts before switching reads.

Release A implementation order: add migration and backup tooling; create the account, person, alias, relationship, and event tables; backfill from legacy requests with a reconciliation report; change preview and pre-send dedup to query durable events; record the final sent body; route dashboard and API starts through one account coordinator; then change contact reads and the session picker to apply an explicit account filter. Keep a rollback path to the original tables until the migrated reads and a controlled dry run match expectations.

## Durable queue and browser worker

The queue stores a campaign and every target before scheduling. A due target is claimed in a transaction with a lease and a unique run ID. The worker acquires the account browser lease, refreshes the person state and account budget, then calls the existing detect, decide, and execute functions. The worker records the final text actually used, the observed outcome, trace, and screenshot. It releases the browser before another run or inbound sync uses the profile.

There are distinct limits for connection invitations, direct messages, and InMail. The user-set invitation ceiling is at most 100 per account per local calendar day, with a separate configurable rolling seven-day ceiling. Pilot values can be lower. Only confirmed invitation sends count toward the invitation budget; uncertain outcomes reserve capacity until reconciled. A LinkedIn limit warning, login challenge, missing session, or repeated browser failure pauses that account and creates a visible action item. No automatic resend follows a timeout after a possible click.

Queue states are `draft`, `queued`, `leased`, `sending`, `sent`, `skipped`, `needs_review`, `uncertain`, `failed`, and `cancelled`. A crash during `sending` becomes `uncertain`. Reconciliation checks the profile or conversation before a person can be retried. A campaign can be paused, resumed, or cancelled without changing historical results. Schedule time is stored in UTC with the account's IANA timezone and displayed in local time.

The browser adapter stays small and preserves the working selectors and send steps. Changes to the browser path require a captured failing case, a targeted regression test, and a headed test against a controlled account before rollout. The known first-40-character DM duplicate heuristic and broad send confirmation deserve isolated tests; the durable queue must not use either as its primary dedup mechanism.

## Inbound sync and files

A read-only daily job checks each active account's relevant conversations and sent invitations. First release scopes detailed message sync to LinkBound contacts, with a visible count and review route for unmatched inbox items. Repeated syncs must be idempotent. The UI distinguishes LinkedIn unread from LinkBound unreviewed. Accepted is recorded only from positive evidence such as first-degree state or an explicit invitation result. The sync records when a section could not be checked rather than presenting old data as current.

For attachments, the worker saves the downloaded bytes before closing the browser, computes a checksum, validates file type and size, and links the file to its source message. The UI can inspect one file or export a filtered ZIP with a contact CSV, message JSON, original files, checksums, and provenance. File names never become storage paths directly. A failed download remains visible and retryable without duplicating the message. Resume import initially supports the file types that Cruitical actually accepts; other shared files stay available in LinkBound.

## Cruitical boundary

Receiving a file or accepting a request creates a LinkBound CRM event. It does not by itself create a Cruitical user. Promotion requires a defined candidate permission rule, a verified identity and email, a supported resume file, and an idempotent delivery record. Build a narrow machine-authenticated Cruitical endpoint that accepts a LinkBound source ID and provenance, checks for an existing candidate, and returns an existing or newly created candidate ID. Conflicts go to review. Never store a human WorkOS admin token in LinkBound or blindly retry an upload that may overwrite a resume.

## Security and operations

Run the control plane behind private HTTPS access, initially Tailscale Serve restricted to Shubham's devices. Add an app session for the human UI and scoped service tokens for worker and Cruitical integration. Authenticate WebSocket, exports, file downloads, and all run controls. Do not expose VNC, Chrome DevTools, or a browser debugging port publicly. Chrome profile directories are credentials; restrict permissions and keep them outside the repository. Back up the database and attachments to an encrypted offsite location, and handle profile backups as credential material. Verify restore, not just backup creation.

The Linode browser pilot uses a non-root service account, a version-matched Playwright installation, the browser channel actually installed, Xvfb for headed Chrome, a persistent profile directory per account, and private remote desktop access for login challenges. Browser location and last successful login are visible in the UI. A process restart reclaims expired leases and marks possible sends `uncertain`; it never restarts them blindly.

## UI direction

Keep the existing campaign preview wizard. Make account context explicit throughout the app. The primary navigation becomes Overview, Campaigns, Inbox, Contacts, Activity, Accounts, and Settings. Overview leads with replies, files, login faults, and uncertain sends needing attention, followed by today's queue per account. Contacts has an explicit All accounts or named account filter, lifecycle facets, campaign filter, server pagination, and a detail pane with the event timeline, messages, files, and Cruitical promotion state. Campaigns shows draft, queued, running, and finished work with local schedule and remaining account budget. Accounts shows owner, login state, worker location, last sync, and pause control.

Use a restrained operations console treatment: canvas `#F8F8F5`, surface `#FFFFFF`, text `#202820`, muted `#66736A`, Cruitical green `#38613A`, alert `#B45335`, and border `#DCE2DA`. Keep dense tables readable at normal browser zoom. Remove decorative glass and repeated reveal animations. Use real buttons and links, keyboard focus, responsive layouts, and explicit loading, empty, stale, and error states. Status color always has a text label and timestamp.

## Delivery sequence and proof

| Release | Independently usable result | Required proof |
| --- | --- | --- |
| A. Foundation | Versioned migrations, account-scoped CRM, immutable send history, normalized identities, single run coordinator | Two accounts retain separate histories; a later skip cannot erase a prior send; old rows reconcile; no current send regression |
| B. Private control plane | Authenticated Linode web app, persistent data, worker protocol, backups, health and restore | Unauthenticated UI/API/file/WebSocket denied; restart preserves CRM; restore succeeds; local worker completes a controlled dry run |
| C. Scheduled outbound | Durable campaigns, per-account budgets, queue UI, pause and crash recovery | 500 targets survive restart; one target cannot be claimed twice; daily and rolling-week ceilings hold; uncertain sends do not auto-retry |
| D. Hosted browser pilot | Headed Linode worker with one account, login recovery, read-only navigation, optional gradual sends | Repeated login and read-only navigation succeed; no profile sharing; account owner reviews pilot evidence before scheduled sends move |
| E. Closed-loop CRM | Daily inbound sync, accepted and reply evidence, attachments, inbox triage, bulk export | Two scans create no duplicate messages or files; stale sync is visible; exported bytes match hashes and source records |
| F. Cruitical promotion | Explicit candidate promotion policy and narrow idempotent integration | Duplicate delivery yields one candidate; missing email or permission stays in review; existing resume is not overwritten silently |

The first implementation branch should contain Release A only. Later releases depend on its data contracts and can each ship independently. The browser pilot is a gate for moving sends, not a prerequisite for making the CRM and queue available on Linode.

## Founder decisions requested after this plan

1. Should the same person be suppressed across all LinkedIn accounts by default, or only within each account? Recommended: global suppression for outbound, with an explicit reviewed override.
2. Should the first inbound sync cover only LinkBound campaign contacts, or every conversation in each account? Recommended: campaign contacts first, with unmatched conversations listed for review.
3. What counts as permission to add a candidate to Cruitical's network after a resume arrives? Recommended: explicit candidate agreement or a reviewed promotion until the permission wording and API contract are settled.
4. Is a Linode VM browser pilot acceptable even though a headed browser on a new IP cannot preserve the current account behavior as a guarantee? Recommended: deploy the control plane first and keep the known local worker until the pilot is reviewed.
5. Is private Tailscale access acceptable for the internal web UI, or must it open in any browser without a VPN client? Recommended: Tailscale for the first deployment.

## Primary references

- [LinkedIn prohibited software](https://www.linkedin.com/help/linkedin/answer/a1341387) and [invitation limits](https://www.linkedin.com/help/linkedin/answer/a550555)
- [Playwright persistent browser contexts](https://playwright.dev/python/docs/api/class-browsertype), [headed Linux CI](https://playwright.dev/docs/ci), [Docker guidance](https://playwright.dev/python/docs/docker), and [downloads](https://playwright.dev/python/docs/api/class-download)
- [Akamai compute plans](https://techdocs.akamai.com/cloud-computing/docs/how-to-choose-a-compute-instance-plan), [backup service](https://techdocs.akamai.com/cloud-computing/docs/backup-service), and [cloud firewall](https://techdocs.akamai.com/cloud-computing/docs/create-a-cloud-firewall)
- [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) and [access control](https://tailscale.com/docs/features/access-control)
- Cruitical product repository `origin/main` at `d946a75e`, especially `apps/backend/api/admin_users.py`, `apps/backend/core/resume.py`, and `apps/backend/core/candidate_analysis.py`
