# Encrypted offsite backup for the Shubham pilot

LinkBound already makes and verifies local snapshots of its SQLite database and
saved files. The offsite job uses restic to encrypt and upload one such snapshot
to a private S3-compatible Akamai Object Storage bucket. Chrome login profiles
are excluded because they contain active session credentials. A lost profile
requires a new login in the private browser viewer.

## Provision once

1. Create a private Object Storage bucket. Create a separate **Limited Access**
   key with Read/Write permission for that bucket. Record the bucket's S3
   endpoint. The [Akamai access-key guide](https://techdocs.akamai.com/cloud-computing/docs/manage-access-keys)
   explains limited keys; its secret is shown only when created.
2. Install the Ubuntu `restic` package on the Linode. Place the access key and
   secret directly on the Linode in `/etc/linkbound/backup.env`, owned by root
   with mode 0600. Do not commit the file or send its contents in chat. Example
   with placeholders:

   ```sh
   RESTIC_REPOSITORY=s3:https://S3_ENDPOINT/BUCKET/linkbound
   RESTIC_PASSWORD_FILE=/etc/linkbound/restic.password
   AWS_ACCESS_KEY_ID=ACCESS_KEY
   AWS_SECRET_ACCESS_KEY=SECRET_KEY
   ```

3. Generate a separate strong restic repository password in
   `/etc/linkbound/restic.password`, owned by root with mode 0600. Preserve a
   copy in the founders' password manager outside the Linode. Without this
   password the encrypted backup cannot be restored.
4. Initialize and test the repository:

   ```sh
   set -a
   . /etc/linkbound/backup.env
   set +a
   restic init
   bash /opt/linkbound/current/scripts/backup_offsite.sh
   restic snapshots
   ```

   The job creates a SQLite online backup, copies the retained file trees,
   verifies the manifest and hashes, uploads an encrypted restic snapshot, and
   runs `restic check`. It uses a lock so two copies cannot overlap. It deletes
   only its own temporary staging directory after completion.

## Prove restore before enabling the timer

Restore into a new empty directory, then verify the recovered LinkBound
manifest and SQLite integrity:

```sh
set -a
. /etc/linkbound/backup.env
set +a
restore_dir="$(mktemp -d /var/lib/linkbound/backups/offsite-restore.XXXXXXXX)"
restic restore latest --target "$restore_dir"
find "$restore_dir/var/lib/linkbound/backups" -name manifest.json -print
/opt/linkbound/current/.venv/bin/python \
  /opt/linkbound/current/scripts/backup_state.py verify \
  "$restore_dir/var/lib/linkbound/backups/OFFSITE_STAGE_NAME/snapshot"
```

Use the stage name printed by `find`. The restore stays separate from the live
database. Record the restored snapshot ID and verification result, then remove
the temporary restore only after confirming it is under
`/var/lib/linkbound/backups/offsite-restore.*`.

After the restore drill, install `deploy/systemd/linkbound-offsite-backup.service`
and `.timer` under `/etc/systemd/system`, run `systemctl daemon-reload`, then
`systemctl enable --now linkbound-offsite-backup.timer`. It runs at 20:30
America/Los_Angeles with up to 15 minutes of jitter, after the 18:00 inbound
sync. Check `systemctl list-timers linkbound-offsite-backup.timer` and the
service journal after the first automatic run. The timer is deliberately not
enabled by a normal code deployment because it requires the private bucket,
credential, and successful restore proof first.

The bucket protects against loss of the VM's local disk. Restic encrypts data
before upload; the repository password and bucket credentials have different
roles. The [restic repository guide](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)
documents S3-compatible repositories and their password requirement.
