# Local dashboard

Run from the project root:

```bash
.venv/bin/python -m dashboard.app
```

Open http://127.0.0.1:8001 in your browser. The gateway can continue on port 8000.
The viewer binds only to localhost, uses read-only SQLite queries, and does not
initialize or migrate data. Create the database via normal proxy setup first.
Use Refresh data for a new snapshot. No external assets or provider calls occur.

Shows all-time recorded usage, estimated spend and savings, safe key IDs and
provider permissions, and the latest 30 ledger rows. It never selects key hashes,
key prefixes, credentials, prompts, or cached response bodies.

This is for a trusted single-user computer, not a public admin service. Any local
process/user able to reach it may open the page; the session cookie is browser
isolation, not user authentication. Host, origin, fetch-site, and loopback checks
reduce browser cross-site exposure. Do not expose it through tunneling, port
forwarding, or a reverse proxy. Public deployment requires real administrator
authentication and HTTPS. There are no key-management or write endpoints.

Middleware rejections and failed log writes are absent from usage totals.
Cache hits record zero new provider tokens. Cost figures are estimates, not invoices.
Snapshot availability is not a gateway health check. Missing database/schema
returns an error instead of creating files. Large all-time aggregations may be
slow as the ledger grows; this initial local viewer has no pagination beyond
the bounded recent-activity list.
