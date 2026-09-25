#!/usr/bin/env bash
# Install one reviewed release without overlapping browser-owning app processes.
set -euo pipefail
umask 022

if [[ "${EUID}" -ne 0 || $# -ne 2 || ! "$1" =~ ^[0-9a-f]{7,40}$ ]]; then
  echo "Usage (as root): deploy_linode.sh COMMIT_SHA /absolute/path/release.tar" >&2
  exit 2
fi

sha="$1"
archive="$2"
release="/opt/linkbound/releases/$sha"
current="/opt/linkbound/current"
env_file="/etc/linkbound/app.env"
unit_file="/etc/systemd/system/linkbound-app.service"
[[ "$archive" = /* && -f "$archive" && ! -e "$release" && -f "$env_file" ]] || {
  echo "Archive, fresh release path, or /etc/linkbound/app.env is missing" >&2
  exit 2
}

# Keep hosted sends disabled until a controlled headed regression verifies
# the stop controls on the server.
set -a
source "$env_file"
set +a
[[ "${LINKBOUND_REQUIRE_TAILSCALE_AUTH:-}" == "true" &&
   "${LINKBOUND_REQUIRE_EXIT_NODE:-true}" == "true" &&
   "${LINKBOUND_ALLOW_LIVE_SENDS:-}" == "false" &&
   "${LINKBOUND_DATA_DIR:-}" = /* &&
   "${LINKBOUND_PROFILE_ROOT:-}" = /* ]] || {
  echo "Private auth, persistent paths, and the no-send gate must be configured" >&2
  exit 2
}
if [[ "${LINKBOUND_ALLOW_TAILNET_DEVICES:-false}" != "true" &&
      -z "${LINKBOUND_TAILSCALE_ALLOWED_USERS:-}" ]]; then
  echo "Configure tailnet-device access or a Tailscale login allowlist" >&2
  exit 2
fi
owner_login="${LINKBOUND_TAILSCALE_ALLOWED_USERS:-}"
owner_login="${owner_login%%,*}"
[[ "$owner_login" != *'['* && "$owner_login" != *']'* ]] || {
  echo "Replace the Tailscale login placeholder before deployment" >&2
  exit 2
}
health_headers=()
if [[ -n "$owner_login" ]]; then
  health_headers=(-H "Tailscale-User-Login: $owner_login")
fi

if systemctl is-active --quiet linkbound-phase0-pilot.service; then
  echo "Stop the Phase 0 pilot browser before deploying the app" >&2
  exit 1
fi
systemctl is-active --quiet linkbound-display.service
systemctl is-active --quiet linkbound-window-manager.service
systemctl is-active --quiet linkbound-novnc.service

previous="$(readlink -f "$current" || true)"
was_active=0
if systemctl is-active --quiet linkbound-app.service; then
  was_active=1
  # Active browser work must finish before the old process is stopped.
  status="$(curl --silent --show-error --fail --max-time 5 \
    "${health_headers[@]}" http://127.0.0.1:8000/api/v1/health)"
  if ! python3 -c 'import json,sys; s=json.load(sys.stdin); sys.exit(1 if s.get("busy") is not False else 0)' <<< "$status"; then
    echo "A browser operation is active; wait for it to finish" >&2
    exit 1
  fi
fi

unit_backup=""
had_unit=0
stopped=0
release_created=0

rollback() {
  result=$?
  trap - ERR
  if [[ "$stopped" -eq 1 ]]; then
    systemctl stop linkbound-app.service || true
    if [[ -n "$previous" ]]; then
      ln -sfn "$previous" "${current}.next"
      mv -Tf "${current}.next" "$current"
    else
      rm -f "$current"
    fi
    if [[ "$had_unit" -eq 1 ]]; then
      cp "$unit_backup" "$unit_file"
    else
      systemctl disable linkbound-app.service || true
      rm -f "$unit_file"
    fi
    systemctl daemon-reload
    if [[ "$was_active" -eq 1 ]]; then
      if [[ -n "$previous" && -f "$previous/deploy/exit-node-gate.v1" ]]; then
        systemctl start linkbound-app.service || true
      else
        # An old release may open Chrome through the Linode's direct route.
        systemctl disable linkbound-app.service || true
        echo "Previous release lacks the exit-node gate; app left stopped after rollback." >&2
      fi
    fi
  fi
  if [[ "$release_created" -eq 1 ]]; then
    rm -rf -- "$release"
  fi
  if [[ -n "$unit_backup" ]]; then
    rm -f "$unit_backup"
  fi
  echo "Deployment failed; previous release restored. Snapshot remains on disk." >&2
  exit "$result"
}
trap rollback ERR

mkdir -p "$release"
release_created=1
tar -xf "$archive" -C "$release"
chown -R root:root "$release"
chmod -R go-w "$release"
test -f "$release/requirements-linux.lock"
[[ ! -e "$release/.env" ]] || {
  echo "Release contains a .env that could override the protected service configuration" >&2
  false
}
python3 -m venv "$release/.venv"
"$release/.venv/bin/pip" install --disable-pip-version-check -r "$release/requirements-linux.lock"
"$release/.venv/bin/python" -m compileall -q "$release/app" "$release/scripts"

unit_backup="$(mktemp)"
if [[ -f "$unit_file" ]]; then
  cp "$unit_file" "$unit_backup"
  had_unit=1
fi

if [[ "$was_active" -eq 1 ]]; then
  systemctl stop linkbound-app.service
fi
stopped=1
# Give the existing non-root service user control of this machine's local
# Tailscale preference. This does not change any tailnet policy or other node.
tailscale set --operator=linkbound
sudo -u linkbound tailscale status --json >/dev/null
snapshot="none"
if [[ -f "$LINKBOUND_DATA_DIR/outbound.db" ]]; then
  mkdir -p /var/lib/linkbound/backups
  chmod 700 /var/lib/linkbound/backups
  snapshot="/var/lib/linkbound/backups/pre-deploy-${sha}-$(date -u +%Y%m%dT%H%M%SZ)"
  backup_args=(snapshot --database "$LINKBOUND_DATA_DIR/outbound.db" --output "$snapshot")
  if [[ -d "$LINKBOUND_DATA_DIR/attachments" ]]; then
    backup_args+=(--attachments "$LINKBOUND_DATA_DIR/attachments")
  fi
  if [[ -d "$LINKBOUND_DATA_DIR/screenshots" ]]; then
    backup_args+=(--screenshots "$LINKBOUND_DATA_DIR/screenshots")
  fi
  if [[ -d "$LINKBOUND_DATA_DIR/uploads" ]]; then
    backup_args+=(--uploads "$LINKBOUND_DATA_DIR/uploads")
  fi
  if [[ -d "$LINKBOUND_DATA_DIR/inbound_files" ]]; then
    backup_args+=(--inbound-files "$LINKBOUND_DATA_DIR/inbound_files")
  fi
  "$release/.venv/bin/python" "$release/scripts/backup_state.py" "${backup_args[@]}"
  "$release/.venv/bin/python" "$release/scripts/backup_state.py" verify "$snapshot"
fi

install -m 0644 "$release/deploy/systemd/linkbound-app.service" "$unit_file"
systemctl daemon-reload
ln -sfn "$release" "${current}.next"
mv -Tf "${current}.next" "$current"
systemctl enable linkbound-app.service
systemctl start linkbound-app.service

ready=0
for attempt in $(seq 1 30); do
  if curl --silent --show-error --fail --max-time 2 \
      "${health_headers[@]}" \
      http://127.0.0.1:8000/api/v1/health | \
      python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("ok") is True else 1)' 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
[[ "$ready" -eq 1 ]]
systemctl is-active --quiet linkbound-app.service
"$release/.venv/bin/python" -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); assert c.execute("PRAGMA user_version").fetchone()[0] == 11' "$LINKBOUND_DATA_DIR/outbound.db"

mkdir -p /var/log/linkbound
printf '%s commit=%s previous=%s snapshot=%s result=success\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$sha" "${previous:-none}" "$snapshot" \
  >> /var/log/linkbound/deploy.log
chmod 600 /var/log/linkbound/deploy.log
rm -f "$unit_backup"
trap - ERR
echo "Deployed $sha with private auth and live sends disabled"
