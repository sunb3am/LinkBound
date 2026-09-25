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

## Remaining live check

After an owner laptop is advertised and approved as an exit node, select it
as LinkBound's default. Run a no-send route pilot to confirm the observed
public IP, IPv4 and IPv6 routes, private app/viewer/SSH reachability, Chrome
login state, and behavior when the node becomes unavailable. Do not enable
the paused daily inbox job or hosted sends merely because the route works;
their separate account-identity and send regression gates still apply.

The initial automatic approval review rejected pushing this private branch
until the user explicitly authorized the destination and payload. The user
granted that approval on 2026-09-25. Branch
`codex/linkbound-exit-node-selection` is published to the configured GitHub
remote. The Linode runs code commit `41ee9ae`; later branch commits only
update this deployment record.
