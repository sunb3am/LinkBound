# Release B deployment record

Observed on 2026-09-23 UTC. This is a partial Release B deployment. Live
LinkedIn sends remain disabled.

## Installed and checked

- Deployed commit `31697d99f933f9188cbcadca45f618065c7b040f` to the Linode
  release directory. The `current` symlink points to it. The single-worker
  `linkbound-app` service, Xvnc display, and noVNC viewer are active.
- The app listens on `127.0.0.1:8000`; raw VNC and websockify also listen on
  loopback. Tailscale Serve maps the app to HTTPS port 443 and keeps the viewer
  on HTTPS port 8443.
- A direct app request without the identity header returned HTTP 403. A
  loopback request with the configured owner identity returned HTTP 200. The
  app configuration reported `live_sends_enabled: false`.
- A deliberately broken release that compiled but failed during startup was
  rejected. The script restored the prior release symlink and unit, restarted
  the app, removed the failed release directory, and the health endpoint
  returned HTTP 200. The failed release made no LinkedIn calls.
- The pre-deployment state snapshot from that rollback drill passed manifest,
  SHA256, and SQLite integrity checks. It restored into a new root-only
  directory with mode 0700 and database mode 0600.
- On the Windows source machine, a new snapshot of the existing local CRM and
  455 screenshots passed verification and restored all 455 screenshots. A
  separate earlier migration rehearsal on a restored database reached schema
  version 2 with matching request counts; the source database was not changed.

## Remaining gates

- The operator device can still reach the viewer on port 8443, but app HTTPS
  on port 443 timed out. The tailnet grant needs TCP 443 added; then verify a
  real Serve request supplies the allowed identity and the app loads.
- The hosted database is currently new and contains only seeded configuration.
  The local database has sender keys `me`, `yt`, and historical
  `yash_thakkar`. Confirm their LinkedIn account ownership and the account
  signed in under the hosted `profiles/me` before importing history.
- A scheduled encrypted offsite database and file backup still needs a
  destination. The on-host snapshot and restore drill are complete.
- The headed browser login persisted in the Phase 0 pilot. Release B has not
  run a hosted outbound campaign. The risk review and Release C stop controls
  remain gates before enabling hosted sends.

## Packaging correction

The first staged Git archive turned the deploy script into CRLF text because
it was created on Windows. Bash rejected it before installation. The branch
now has `.gitattributes` rules that keep shell scripts and systemd units LF in
archives. The corrected archive passed `bash -n` on the Linode before deploy.
