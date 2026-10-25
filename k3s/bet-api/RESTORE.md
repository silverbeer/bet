# Backups and restore

## What runs

| When | Where | What |
|---|---|---|
| 03:30 America/New_York nightly | mini, CronJob `bet-api-backup` | `POST /maintenance/backup?keep_days=14`: the API writes a consistent copy to `/data/backups/bet-<UTC stamp>.duckdb` and prunes copies older than 14 days |
| hourly, while the Air is awake | MacBook Air, launchd `io.silverbeer.bet.backup-pull` | `scripts/pull-backup.sh` copies the newest backup to `~/iCloud/bet/backups-mini/` (sha256-checked) and prunes copies there older than 30 days |

**Why the API makes the copy.** The API is the warehouse's only process: DuckDB
lets one process hold the file. A raw file copy taken while it runs can catch a
half-written checkpoint. The endpoint runs `EXPORT DATABASE` (one snapshot of
every table), imports that into a fresh file at the pinned storage version and
renames it into place. Writes during a backup are not blocked and are not in it.

The maintenance token (Secret `bet-api-maintenance`) is not a user login. It
opens only `/maintenance/*`, and no user token opens those. A backup holds
everything, password and refresh-token hashes included, so treat the files and
the token like the warehouse itself.

## Checking

```bash
# on the mini
kubectl -n bet get cronjob bet-api-backup                 # LAST SCHEDULE
kubectl -n bet logs job/$(kubectl -n bet get jobs -o name | tail -1 | cut -d/ -f2)
./scripts/restore-backup.sh --list                        # files on the volume

# on the Air
scripts/pull-backup.sh --check
tail ~/Library/Logs/bet-backup-pull.log
```

Run a backup now: `kubectl -n bet create job --from=cronjob/bet-api-backup backup-now`.

## Restore

On the mini, from the repo checkout:

```bash
./scripts/restore-backup.sh bet-20261025T073000Z.duckdb              # a file on the volume
./scripts/restore-backup.sh --file ~/Downloads/bet-20261025T073000Z.duckdb  # a file on this Mac
```

To restore from the Air's iCloud copy, copy it to the mini first
(`scp ~/iCloud/bet/backups-mini/<file> mini:`). Then use `--file`.

The script:

1. scales `bet-api` to zero, so nothing holds the file;
2. starts a helper pod on the `bet-data` volume;
3. with `--file`, uploads the file into `/data/backups` and checks its sha256;
4. moves the live `bet.duckdb` aside as `bet.duckdb.pre-restore-<stamp>` (never
   deleted by the script) and copies the backup into place;
5. runs `bet doctor` against it. **If doctor fails, it puts the previous file
   back** before starting the API again;
6. scales `bet-api` back to 1 and waits for `/readyz`.

Sessions survive a restore if the backup holds their refresh tokens. Logins made
after the backup was taken have to `bet login` again.

### Identity: restoring a different warehouse

`bet init` records the warehouse's tenant and user ids in
`/data/config/bet/config.toml`, and `bet doctor` fails when the database does
not contain them. Backups of this warehouse always match. A file from somewhere
else does not, and the restore refuses it and rolls back (seen in the test
below). That includes the first cutover from the Air (SB-1405). The cutover
must also bring the config's identity across.

## Tested end to end (2026-10-25)

On the mini, against the then-fresh warehouse, with the SB-1404 image:

- CronJob triggered by hand: `bet-20261025T032039Z.duckdb`, 55 rows; pulled to
  the Air with a matching sha256; a second pull was a no-op.
- `--file` restore of a backup of the Air's warehouse: doctor failed on identity
  (as above). The script restored the previous file, and a backup afterwards
  still showed 55 rows.
- Restore by name of `bet-20261025T032039Z.duckdb`: doctor passed, the API came
  back ready, and the previous file was kept as `bet.duckdb.pre-restore-…`.
