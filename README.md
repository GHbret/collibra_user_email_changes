# collibra-email-audit

Polls the Collibra Core REST API (v2) `/rest/2.0/users` endpoint and reports each user's current email address and when their profile was last modified. Detects and flags email address changes by comparing snapshots across runs.

## Why

Collibra's Users API returns the *current* state of a user profile plus a `lastModifiedOn` timestamp — it doesn't expose a field-level change history (e.g. "email changed from X to Y on this date"). This script closes that gap by snapshotting user data on each run and diffing against the previous snapshot to surface actual email changes.

## Requirements

- Python 3.9+
- A Collibra account with the **User Administration** or **System Administration** global permission (required to view other users' email addresses and modification metadata)
- Network access to your Collibra tenant

## Setup

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install requests
```

## Configuration

Set your Collibra credentials as environment variables:

```bash
export COLLIBRA_BASE_URL="https://yourinstance.collibra.com"
export COLLIBRA_USERNAME="your_username"
export COLLIBRA_PASSWORD="your_password"
```

Or pass them as CLI flags (see below).

## Usage

```bash
python collibra_email_audit.py
```

Optional flags:

| Flag | Default | Description |
|---|---|---|
| `--base-url` | `$COLLIBRA_BASE_URL` | Collibra instance URL |
| `--username` | `$COLLIBRA_USERNAME` | Account username |
| `--password` | `$COLLIBRA_PASSWORD` | Account password |
| `--output` | `collibra_user_emails.csv` | Path to write the CSV report |
| `--state` | `collibra_user_email_state.json` | Snapshot file used to detect changes between runs |
| `--changelog` | `collibra_email_changelog.txt` | Plain-text log appended to whenever email changes are detected |

## What it does

On each run, the script:

1. Authenticates against Collibra (`POST /rest/2.0/auth/sessions`).
2. Pages through `GET /rest/2.0/users` to fetch every user.
3. Writes a CSV report with one row per user.
4. Compares the current snapshot to the previous run's snapshot (`--state` file) and flags any user whose email address differs.
5. Overwrites the state file with the current snapshot for the next run.

## Output

**CSV columns:**

| Column | Description |
|---|---|
| `user` | Username |
| `email` | Current email address |
| `last_modified_on` | When the profile was last modified, converted from Collibra's raw Unix-epoch-milliseconds into `YYYY-MM-DD HH:MM:SS UTC` |
| `last_modified_by` | Username of whoever made the last change, resolved from the raw modifier UUID (falls back to the UUID if that account no longer exists, e.g. a system account) |
| `email_changed_since_last_run` | `True` if this user's email differs from the previous run's snapshot |

Any detected change is also printed to stdout as:

```
CHANGED: jdoe email is now 'jane.doe@newdomain.com' (was 'jdoe@olddomain.com'), last modified 2026-07-24 14:03:11 UTC by asmith
```

**Changelog file:** the same lines are appended to `collibra_email_changelog.txt` (or your `--changelog` path) under a timestamped run header, so you keep a running history of every detected change across runs:

```
=== Run: 2026-07-24 14:03:20 UTC ===
CHANGED: jdoe email is now 'jane.doe@newdomain.com' (was 'jdoe@olddomain.com'), last modified 2026-07-24 14:03:11 UTC by asmith
```

Nothing is written to the changelog on runs where no changes are detected.

**Note:** on the very first run there's no prior snapshot to compare against, so no changes will be flagged — that run just establishes the baseline.

## Limitations

- Change detection is based on comparing snapshots between runs, not a true audit trail. If an email changes and changes back between two runs, it won't be caught.
- Only detects changes for users returned by `/rest/2.0/users` at the time of each run (e.g. a deleted user's final change won't be visible after the fact).
- Response schema (`results` key) is based on Collibra's v2 API; verify against your instance's live OpenAPI docs at `{base_url}/rest/2.0/docs` if you hit parsing errors.

## Automating

Run this on a schedule (cron, Task Scheduler, CI job, etc.) to poll periodically:

```bash
# crontab example: run every hour
0 * * * * cd /path/to/project && source venv/bin/activate && python collibra_email_audit.py >> audit.log 2>&1
```

## License

MIT (or update to your preference).
