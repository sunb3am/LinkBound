# Release B: private Linode app

Release B runs one LinkBound process on the existing headed Linode display. The
viewer remains on Tailscale Serve port 8443. The app uses Serve port 443 to
forward to Uvicorn on `127.0.0.1:8000`. The public firewall does not expose
either backend. This release keeps live sends disabled while the access review,
account mapping, and queue stop controls are completed.

## Persistent paths and access

- Code: `/opt/linkbound/releases/<commit>` with `/opt/linkbound/current` as the
  active symlink. A deployment never writes runtime state into a code release.
- State: `/var/lib/linkbound/data/outbound.db`, plus `screenshots`, `uploads`,
  and future `attachments` below the same data directory. Chrome profiles stay
  in `/var/lib/linkbound/profiles/<sender>` and are owned by `linkbound`.
- Configuration: `/etc/linkbound/app.env`, owned by root, group `linkbound`, mode
  0640. Set `LINKBOUND_REQUIRE_TAILSCALE_AUTH=true` and
  `LINKBOUND_ALLOW_TAILNET_DEVICES=true` to make tailnet reachability the web
  access decision. Retain `LINKBOUND_ALLOW_LIVE_SENDS=false` during the guarded
  pilot. Set `LINKBOUND_PILOT_DAILY_CAP=5` and
  `LINKBOUND_PILOT_WEEKLY_CAP=20` as temporary Shubham ceilings before any
  hosted send. The old per-login allowlist remains an optional narrower mode.
- The app binds only to `127.0.0.1:8000`. In tailnet-device mode, it accepts
  HTTP and WebSocket requests only from the local Tailscale Serve proxy, without
  checking a login name. Tailscale Serve on ports 443 and 8443 stays private;
  Funnel must remain off. Local trusted host processes can reach the loopback
  port, so host access remains restricted.
- Grant direct tailnet members and tagged tailnet devices access to this Linode
  on TCP 443 and TCP 8443. Do not widen TCP 22 SSH access. A grant using
  `autogroup:member` and `autogroup:tagged` excludes shared users and subnet
  routes. Check `tailscale serve status` after a policy change and test from a
  second member device.

Add this grant to the existing tailnet policy's `grants` array. Keep the current
SSH grant separate:

```json
{
  "src": ["autogroup:member", "autogroup:tagged"],
  "dst": ["100.103.144.62"],
  "ip": ["tcp:443", "tcp:8443"]
}
```

Before the first hosted send and after browser upgrades, record the real OS,
timezone, locale, Chrome and Playwright versions, display, public egress IP,
profile owner and mode, and observed login challenges. Compare with the prior
record. Do not replace those values with a fabricated laptop identity.
The Linux helper prints a JSON snapshot without cookies or profile contents:

```bash
sudo -u linkbound env DISPLAY=:1 \
  /opt/linkbound/current/.venv/bin/python \
  /opt/linkbound/current/scripts/capture_environment_baseline.py \
  --account me --profile /var/lib/linkbound/profiles/me \
  --challenge not_observed
```

Save its output under `/var/lib/linkbound/evidence/` with a timestamp and
compare it to the preceding record before a hosted send. If the egress IP,
account binding, or browser environment changed unexpectedly, inspect the
headed session first.

## Deployment

Deploy a reviewed Git commit, not a dirty checkout. From a machine with SSH
access to the Linode, create a Git tar archive of that commit, copy it and
`deploy/deploy_linode.sh` to a root-only staging directory on the host, then
run `bash deploy_linode.sh COMMIT_SHA /absolute/path/release.tar` as root.

The script requires the display, window manager, and viewer services to be
active; it refuses to run while the Phase 0 pilot or any app browser operation
is active. It builds a fresh virtual environment from `requirements-linux.lock`,
stops the old app, snapshots the database and retained files, switches the
release symlink, starts exactly one Uvicorn worker, and checks health and schema
version 9. A failed deployment restores the previous unit and release link and
does not automatically resume an interrupted browser operation. The deployment
log is `/var/log/linkbound/deploy.log`.

Use `scripts/backup_state.py snapshot`, `verify`, and `restore` for a manual
snapshot or restore drill. Restore always writes to a new directory so the
existing state cannot be overwritten accidentally. SQLite is copied with its
online backup API; the manifest records SHA256 hashes of the database and file
bytes. Profile directories are deliberately excluded from ordinary CRM backups
because they contain authenticated LinkedIn sessions. Recovering a lost profile
may require signing in again through the private viewer.

## Current release gates

The first app deployment may start with a new empty database. Import the local
CRM only after confirming which account owns each historical sender key and
which login uses `/var/lib/linkbound/profiles/me`. Stop the app before replacing
the empty database, retain a verified pre-import snapshot, and start it only
after a restore drill on the imported copy. Do not merge `yt` and
`yash_thakkar` based on their names alone.

The deployment snapshot is on the VM. Follow the
[offsite backup runbook](linkbound-offsite-backup.md) to provision the private
bucket, prove an encrypted restore, and enable the backup timer. Until then the
VM is the only verified location for current CRM data. The no-send gate remains in `/api/start`, `/api/v1/enqueue`,
and the shared run coordinator. The operator has authorized hosted use for the
initial Shubham account. A controlled headed regression of challenge, limit,
and uncertain-send stops remains necessary before enabling hosted sends.
