#!/usr/bin/env bash
# Copy a verified SQLite-consistent LinkBound snapshot into an encrypted restic repository.
set -euo pipefail
umask 077

config="${LINKBOUND_BACKUP_ENV:-/etc/linkbound/backup.env}"
if [[ ! -f "$config" || "$(stat -c %u "$config")" != 0 ||
      "$(stat -c %a "$config")" != 600 ]]; then
  echo "Backup configuration must be a root-owned mode-600 file: $config" >&2
  exit 1
fi
set -a
# The root-owned configuration contains RESTIC_REPOSITORY, RESTIC_PASSWORD_FILE,
# and, for S3, AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.
source "$config"
set +a
: "${RESTIC_REPOSITORY:?Set RESTIC_REPOSITORY in the backup configuration}"
: "${RESTIC_PASSWORD_FILE:?Set RESTIC_PASSWORD_FILE in the backup configuration}"
if [[ ! -f "$RESTIC_PASSWORD_FILE" ||
      "$(stat -c %u "$RESTIC_PASSWORD_FILE")" != 0 ||
      "$(stat -c %a "$RESTIC_PASSWORD_FILE")" != 600 ]]; then
  echo "The restic password file must be root-owned and mode 600" >&2
  exit 1
fi

exec 9>/run/lock/linkbound-offsite-backup.lock
flock -n 9 || { echo "Another offsite backup is running" >&2; exit 1; }

current="/opt/linkbound/current"
data="/var/lib/linkbound/data"
stage="$(mktemp -d /var/lib/linkbound/backups/offsite-stage.XXXXXXXX)"
cleanup() {
  if [[ "$stage" == /var/lib/linkbound/backups/offsite-stage.* && -d "$stage" && ! -L "$stage" ]]; then
    rm -rf -- "$stage"
  fi
}
trap cleanup EXIT

args=(snapshot --database "$data/outbound.db" --output "$stage/snapshot")
for name in attachments screenshots uploads inbound_files; do
  if [[ -d "$data/$name" ]]; then
    case "$name" in
      inbound_files) args+=(--inbound-files "$data/$name") ;;
      *) args+=("--$name" "$data/$name") ;;
    esac
  fi
done
"$current/.venv/bin/python" "$current/scripts/backup_state.py" "${args[@]}"
"$current/.venv/bin/python" "$current/scripts/backup_state.py" verify "$stage/snapshot"
restic backup --tag linkbound "$stage/snapshot"
restic check
echo "Encrypted offsite backup and repository check completed"
