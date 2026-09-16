# Local management dashboard

Run from the repository root:

```bash
.venv/bin/python -m dashboard.app
```

Open http://127.0.0.1:8001. Initialize the gateway database first using the normal
setup workflow. Restart an existing dashboard process after updating its Python
code, then reload the browser.

## Screens

- **Overview:** all-time requests, provider tokens, estimated cost, cache hits,
  estimated savings, recorded errors and the latest 30 usage records (UTC).
- **Virtual keys:** create a key with one initial provider, show its secret once,
  grant another supported provider, revoke access with confirmation, view usage
  per key or across keys, and download that usage as JSON.
- **Maintenance:** create maintenance or hourly backups, verify a selected local
  snapshot, preview expired cache entries, and confirm backup-gated deletion.

The interface uses white and gray surfaces with blue primary buttons. Revocation
confirmation uses red to distinguish the destructive action. Refresh is manual.
New secrets are cleared from the form when dismissed and are never stored in
browser storage. Losing the creation response or closing before saving means the
secret cannot be recovered; revoke the unwanted key and create another.

Provider choices are Anthropic and Fireworks with the trusted `default` credential
reference supported by the gateway. A grant does not install an adapter or configure
its credentials. Provider secrets, URLs, cached answers and hashes are never shown.
Revocation retains usage history. Permission removal, key reactivation, provider
configuration editing, gateway process management and live database restoration
are not exposed. The CLI remains available.

## Local trust boundary

This is a **localhost-only administrator tool**, not a public or multi-user admin
service. The cookie and request token provide browser isolation, not user identity;
any process or user that can access this machine's loopback interface may obtain a
session. Do not expose it through a tunnel or reverse proxy.

All administrative endpoints require the session cookie. Writes additionally
require an exact matching Origin, a request token in `X-CSRF-Token`, JSON content,
and a body of at most 16 KB (actual bytes counted). Loopback, Host and cross-site
checks remain enabled. Responses use no-store and the UI renders supplied text
using textContent. No provider calls are made.

Operations reuse CLI database/backup/cleanup helpers with explicit paths. The
database must already exist; normal persistence helpers retain their additive
schema initialization behavior. Backups stay in the configured backup directory;
verification accepts existing regular snapshots, not arbitrary paths. Hourly
backup creation applies existing retention and does not start a schedule. Cache
cleanup recounts expired rows when executed and requires a verified backup before
deleting any. Verification failure prevents deletion. Backup verification restores
only into temporary storage.

Usage excludes middleware rejections and missing log writes. Cost figures are
estimates, not invoices. Cache hits record no new provider tokens. Reading the
database does not establish that the gateway or providers are online.

## Validation

```bash
.venv/bin/python -m unittest tests.test_dashboard tests.test_dashboard_management -v
```

Tests use temporary SQLite databases and backup directories. They cover key
creation/grants/revocation, secret omission, export, access checks, input limits,
backup verification and traversal rejection, and cleanup's backup-failure behavior.
