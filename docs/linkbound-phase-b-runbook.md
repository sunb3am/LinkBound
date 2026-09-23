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
  0640. Start from `deploy/app.env.example`, replace the allowed user with the
  exact Tailscale login, and retain `LINKBOUND_ALLOW_LIVE_SENDS=false`.
- The app binds only to loopback. Tailscale Serve supplies the user login header
  and strips the same header from incoming requests. The app checks that header
  against its allowlist and accepts it only from a loopback proxy connection.
  All HTTP routes, downloads, static files, and WebSockets pass through this
  check. Local root processes can reach the loopback port, so host access is
  restricted to trusted operators.
- The app's Tailscale grant must permit the owner device to reach this Linode
  on TCP 443. Keep the existing TCP 8443 viewer and TCP 22 SSH grants. Configure
  Serve as `tailscale serve --bg --https=443 8000`, then check `tailscale serve
  status` to confirm both endpoints remain present.

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
version 2. A failed deployment restores the previous unit and release link and
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

The deployment snapshot is on the VM. An encrypted offsite destination and a
scheduled copy are still required before treating this host as the only copy of
production CRM data. The no-send gate remains in `/api/start`, `/api/v1/enqueue`,
and the shared run coordinator. A controlled headed regression and explicit
account-owner decision remain prerequisites to hosted sends.
