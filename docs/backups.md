# SQLite backup and restore checks

The policy is hourly snapshots plus an extra snapshot immediately before planned
schema changes or destructive maintenance—not before every usage write.

```bash
.venv/bin/python -m auth.backup create
```

Run this from the repository root before maintenance; proceed only if it succeeds.
It uses SQLite's online backup API, including committed WAL data, rather than a
filesystem copy. The source is opened read-only. A snapshot passes integrity and
foreign-key checks, is restored into a temporary SQLite file, and its schema/data
are compared with the snapshot before publication. No data values are printed.
The comparison is with the snapshot, not the live database which can keep changing.

Snapshots live in the Git-ignored `backups/` directory (mode 700); files use mode
600. Restore tests remove their temporary copy and never replace the live database.
To check an existing snapshot:

```bash
.venv/bin/python -m auth.backup verify --snapshot backups/EXACT-SNAPSHOT-NAME.db
```

Hourly execution is managed separately through the local task automation. It
depends on the host and scheduler being available; it is not a deployment server
service or a guaranteed hourly recovery point. Use a server-native scheduler when
deploying. Failures must be investigated rather than assumed to be successful backups.

Keep all snapshots for now: no automatic retention/deletion has been authorized.
Watch disk use. These files include sensitive key hashes and cached answers; they
are not encrypted. A same-disk backup does not protect against losing the machine
or disk; protected off-machine storage and a retention policy remain future work.
Verification materializes SQL dumps in memory, suitable for this small database;
revisit this for large datasets. The SQLite copy has a sixty-second progress deadline.

Actual disaster recovery (stopping the server, selecting a known-good snapshot,
preserving the damaged database, replacing it, and restarting) remains a separate,
explicit operator action; this command only tests restoration safely.
