# Manual expired-cache cleanup

From the repository root, preview only:

```bash
.venv/bin/python -m auth.cache_cleanup
```

To deliberately delete expired entries:

```bash
.venv/bin/python -m auth.cache_cleanup --delete
```

Deletion first creates and verifies a protected maintenance backup using the existing backup mechanism.
A backup failure prevents deletion. No automatic cleanup is scheduled.
The SQL transaction deletes only cache rows whose stored deadline has passed or
whose age is at least 30 minutes, including old one-hour entries. Keys, permissions,
usage records, rate-limit records, and valid cache entries are not deleted.

The cutoff is fixed at command start; rows expiring during backup wait for another
run. Concurrent traffic can change the preview count; deletion checks row expiry
again inside its transaction. Preview is not a reservation or a backup.
This short write transaction may temporarily contend with other SQLite writers.

SQLite can reuse freed pages; this does not necessarily shrink the database file.
No VACUUM or secure erase is performed. Backups retain their snapshot contents,
including expired answers. Maintenance copies are excluded from hourly retention;
review them separately only after confirming the maintenance worked.
To recover, use the backup instructions and an explicit operator restore procedure;
the cleanup command never replaces the live database automatically.
