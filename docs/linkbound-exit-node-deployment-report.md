# LinkBound exit-node release: deployment record

Recorded 2026-09-25 01:57 UTC. The code release is `41ee9ae` on the LinkBound
Linode at Tailscale IP `100.103.144.62`. The separate agent VPS was not
contacted or changed.

## Delivered

- A saved default exit node for unattended sync and browser work, plus an
  override for manual runs and queued campaigns. A queued campaign can change
  its node between chunks.
- The hosted runner refuses to launch Chrome without an approved, online node.
  It checks the selected Tailscale node, IPv4 and IPv6 routes, and observed
  public IPv4 before opening LinkedIn. A watchdog checks the route while Chrome
  is open. Browser and route cleanup stay serialized through cancellation.
- Batch and inbox-run records store the selected node ID, name, and observed
  public IPv4. The UI shows the default, task selection, blocked state, and
  available route evidence.
- The non-root `linkbound` Unix user is the Tailscale operator on this Linode.
  The app still has `NoNewPrivileges=true`. A failed first deployment cannot
  restart an older release that lacks the exit-node gate.

## Verification

- Local: 162 tests passed. JavaScript and deployment Bash syntax checks passed.
  A local headless Chrome smoke test rendered the blocked state and selectors
  without page errors; it was a UI test, not a LinkedIn browser run.
- The archive SHA256 matched before and after transfer:
  `b49ef7a65288fe49cac06a7c30750770d54b5c29118fb704fcecf29f4963552b`.
- The deployment created and verified the database snapshot at
  `/var/lib/linkbound/backups/pre-deploy-41ee9ae-20260925T015354Z`.
- The deployed release is `/opt/linkbound/releases/41ee9ae`; the app service
  is active; the app and viewer each returned HTTP 200; SQLite is schema 11.
  `linkbound` successfully ran `tailscale set --exit-node=` with no node set.
- Tailscale reported no approved exit nodes. `/api/exit-nodes` returned an
  empty list, an empty default, and `required: true`. A preview of a dummy
  contact URL succeeded, but starting its dry run returned HTTP 409 with
  `Select an online exit node before LinkedIn browser work`. The app stayed
  idle and the Chrome process count was 0.
- Hosted live sends and the daily inbound schedule remained disabled.

## Sunbeam pilot, 2026-09-25

The owner advertised and approved `sunbeam` (`100.71.193.94`), and LinkBound
reported it as an online selectable exit node. Sunbeam's stable node ID was
saved as the default. The first benign route attempt revealed that this
Tailscale CLI accepts an exit node IP or hostname, not its node ID. Commit
`05a2a73` fixed that selector and passed 163 tests. Its deployment created
and verified snapshot
`/var/lib/linkbound/backups/pre-deploy-05a2a73-20260925T101451Z`.

With that release, a benign route check selected Sunbeam and observed public
IPv4 different from the Linode's `50.116.8.251`. The controller also passed
its IPv4 and IPv6 route checks, the private app and viewer returned HTTP 200,
and clearing the route succeeded. A browser-only dry run against the sender's
bound profile finished as batch 7 with `dry_run=1`, `sent=0`, `skipped=1`, and
no failed or flagged items. Its one item was `connect_unavailable`, consistent
with using the sender's own profile; no invitation or message was sent. The batch
recorded Sunbeam and a public IP different from the Linode's. Afterwards,
Tailscale had no selected exit node, the Chrome process count was 0, the app
was idle, and the app and viewer returned HTTP 200.

A read-only name-resolution call returned HTTP 200 but `updated=0`. It is not
evidence that the profile name collector succeeded. A forced exit-node outage
was not tested. The daily inbox job and hosted live sends remain disabled
until their separate account-identity and send regression checks pass.

The initial automatic approval review rejected pushing this private branch
until the user explicitly authorized the destination and payload. The user
granted that approval on 2026-09-25. Branch
`codex/linkbound-exit-node-selection` is published to the configured GitHub
remote. The Linode runs code commit `05a2a73`; later branch commits only
update this deployment record.
