# Shubham's one-time history import

The workstation database contains several operators. The hosted database has
Shubham's live inbox observations but no earlier outbound requests. Import only
the `me` account. This is an additive history migration, not a replacement of
the hosted database or the Chrome profile.

`scripts/import_legacy_shubham.py bundle` takes a consistent SQLite copy,
upgrades that temporary copy, and creates a fresh schema 9 database containing
only Shubham's batches, requests, account contacts, and profile rows. The ZIP
includes only screenshots referenced by those requests and a SHA256 manifest.
It does not carry the other operators, templates, LinkedIn cookies, or Chrome
profile. The source database is read-only throughout.

## Apply gate

1. Deploy the reviewed commit containing the importer. Confirm the app is idle
   and live sends are disabled. Create a scoped bundle on the workstation and
   inspect its JSON count summary. Transfer the ZIP through SSH into a
   root-only directory on the VM. Keep it out of the code release.
2. Stop `linkbound-app.service`. Use `scripts/backup_state.py snapshot` on
   `/var/lib/linkbound/data/outbound.db` with every existing retained file
   directory, then `verify` the snapshot. Keep this pre-import snapshot until
   the imported state and its later offsite copy have been restored in a drill.
3. Run `scripts/import_legacy_shubham.py import` without `--apply` against the
   stopped database and `/var/lib/linkbound/data/screenshots`. Compare the
   summary to the workstation bundle. The importer refuses a target with any
   outbound batches, requests, shared contacts, or account contacts. It also
   requires Shubham's hosted account to have a bound LinkedIn self URL.
4. Run the same command with `--apply`. Confirm SQLite integrity and foreign
   keys, request status counts, screenshot count and hashes, existing inbox
   message/file counts, and the app's account-scoped CRM view. One legacy
   `running` batch becomes `interrupted`; it must never resume automatically.
   Template IDs are cleared because the old template table is not imported.
   When run as root, the importer assigns screenshot ownership to the account
   that owns the hosted database, keeping files private to the app service.
5. Start the app. Confirm health, the Shubham contact list, and an imported
   screenshot. Then run a bounded no-send scan with the existing Chrome profile
   to test one tracked contact's first-degree check. Stop if a challenge or
   restriction appears. Remove the transferred ZIP from the VM after the
   verified snapshot and import proof are retained.

If import verification fails, keep the app stopped. Restore the verified
pre-import snapshot to a new directory, compare its manifest, and only then
replace the affected database and retained files while no service owns them.
The import itself never modifies the Chrome profile or inbound tables. Do not
use the importer a second time to merge new outbound history; it intentionally
refuses a populated target.
