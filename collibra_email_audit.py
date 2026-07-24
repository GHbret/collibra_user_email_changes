#!/usr/bin/env python3
"""
collibra_email_audit.py

Polls the Collibra Core REST API (v2) Users endpoint and reports each user's
current email address and when their profile was last modified. Optionally
diffs against the previous run's snapshot to flag users whose email address
actually changed (the Users endpoint gives you the current value + a
lastModifiedOn timestamp, not a field-level diff, so change detection is
done here by comparing snapshots across runs).

Requires network access to your Collibra tenant and an account with the
"User Administration" or "System administration" global permission (needed
to see other users' email addresses / modification metadata).

Usage:
    pip install requests
    export COLLIBRA_BASE_URL="https://yourinstance.collibra.com"
    export COLLIBRA_USERNAME="svc_account"
    export COLLIBRA_PASSWORD="..."
    python collibra_email_audit.py --output report.csv --state state.json

Each run:
  1. Authenticates and fetches all users.
  2. Writes report.csv with: user, email, last_modified_on, last_modified_by.
     - last_modified_on is converted from Collibra's raw Unix-epoch-milliseconds
       into a readable "YYYY-MM-DD HH:MM:SS UTC" string.
     - last_modified_by is resolved from the raw modifier UUID to that user's
       username by cross-referencing the same user list (falls back to the
       raw UUID if the modifying account no longer exists / isn't in the list,
       e.g. a system account).
  3. Compares to state.json from the previous run; any user whose email
     differs from the prior snapshot is printed to stdout as CHANGED,
     marked in the CSV's `email_changed_since_last_run` column, and appended
     to collibra_email_changelog.txt under a timestamped run header (nothing
     is written to the changelog on runs with no detected changes).
  4. Overwrites state.json with the current snapshot for next time.

Schedule this (cron, Task Scheduler, CI job, etc.) to poll periodically.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

import requests

DEFAULT_PAGE_SIZE = 1000


def authenticate(base_url: str, username: str, password: str) -> requests.Session:
    """Start a Collibra session (cookie-based auth) and return a Session
    object with the auth cookie attached for subsequent requests."""
    session = requests.Session()
    resp = session.post(
        f"{base_url}/rest/2.0/auth/sessions",
        json={"username": username, "password": password},
        timeout=30,
    )
    resp.raise_for_status()
    return session


def fetch_all_users(session: requests.Session, base_url: str, page_size: int = DEFAULT_PAGE_SIZE):
    """Page through GET /rest/2.0/users and return a list of user dicts.

    Collibra's v2 API wraps paginated collections in a `results` list
    alongside `total`/`offset`/`limit`. If your instance's schema differs,
    check the live OpenAPI docs at {base_url}/rest/2.0/docs and adjust the
    key used below.
    """
    users = []
    offset = 0
    while True:
        resp = session.get(
            f"{base_url}/rest/2.0/users",
            params={"limit": page_size, "offset": offset},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            batch = data
        elif isinstance(data, dict) and "results" in data:
            batch = data["results"]
        else:
            raise RuntimeError(
                f"Unexpected response shape from /rest/2.0/users: keys={list(data.keys()) if isinstance(data, dict) else type(data)}"
            )

        if not batch:
            break

        users.extend(batch)

        if len(batch) < page_size:
            break
        offset += page_size

    return users


def epoch_ms_to_utc_string(value) -> str:
    """Convert Collibra's raw lastModifiedOn (Unix epoch milliseconds) into
    a readable 'YYYY-MM-DD HH:MM:SS UTC' string. Returns the raw value
    unchanged if it isn't a parseable number."""
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError):
        return str(value)


def build_snapshot(users):
    """Reduce raw user records to {user_id: {userName, emailAddress, lastModifiedOn,
    lastModifiedBy, lastModifiedOnFormatted, lastModifiedByName}}.

    lastModifiedOnFormatted / lastModifiedByName are derived, human-readable
    versions of the raw epoch-ms timestamp and modifier UUID that Collibra
    returns; the raw values are kept too (used for sorting / state diffing).
    """
    snapshot = {}
    for u in users:
        user_id = u.get("id") or u.get("resourceId")
        if not user_id:
            continue
        snapshot[user_id] = {
            "userName": u.get("userName") or u.get("username") or u.get("fullName") or "",
            "emailAddress": u.get("emailAddress") or "",
            "lastModifiedOn": u.get("lastModifiedOn") or "",
            "lastModifiedBy": u.get("lastModifiedBy") or "",
        }

    # Resolve lastModifiedBy UUIDs to usernames now that we have the full id->name map.
    id_to_name = {uid: info["userName"] for uid, info in snapshot.items() if info["userName"]}
    for info in snapshot.values():
        raw_modifier = info["lastModifiedBy"]
        info["lastModifiedByName"] = id_to_name.get(raw_modifier, raw_modifier)
        info["lastModifiedOnFormatted"] = epoch_ms_to_utc_string(info["lastModifiedOn"])

    return snapshot


def load_previous_state(state_file: str):
    if not os.path.exists(state_file):
        return {}
    with open(state_file, "r") as f:
        return json.load(f)


def write_report(csv_path: str, snapshot: dict, previous: dict, changelog_path: str = None):
    changed_count = 0
    changed_lines = []
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["user", "email", "last_modified_on", "last_modified_by", "email_changed_since_last_run"])
        for user_id, info in sorted(
            snapshot.items(),
            key=lambda kv: int(kv[1]["lastModifiedOn"]) if kv[1]["lastModifiedOn"] else 0,
            reverse=True,
        ):
            prev = previous.get(user_id)
            changed = bool(prev) and prev.get("emailAddress") != info["emailAddress"]
            if changed:
                changed_count += 1
                line = (
                    f"CHANGED: {info['userName']} email is now '{info['emailAddress']}' "
                    f"(was '{prev.get('emailAddress')}'), last modified {info['lastModifiedOnFormatted']} "
                    f"by {info['lastModifiedByName']}"
                )
                print(line)
                changed_lines.append(line)
            writer.writerow([
                info["userName"],
                info["emailAddress"],
                info["lastModifiedOnFormatted"],
                info["lastModifiedByName"],
                changed,
            ])

    # Append any detected changes to a running plain-text changelog, one
    # timestamped block per run. Nothing is written on runs with no changes.
    if changelog_path and changed_lines:
        run_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(changelog_path, "a") as f:
            f.write(f"=== Run: {run_stamp} ===\n")
            for line in changed_lines:
                f.write(line + "\n")
            f.write("\n")

    return changed_count


def main():
    parser = argparse.ArgumentParser(description="Report Collibra user emails and last-modified timestamps; flag changes since last run.")
    parser.add_argument("--base-url", default=os.environ.get("COLLIBRA_BASE_URL"), help="e.g. https://yourinstance.collibra.com")
    parser.add_argument("--username", default=os.environ.get("COLLIBRA_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("COLLIBRA_PASSWORD"))
    parser.add_argument("--output", default="collibra_user_emails.csv", help="CSV report path")
    parser.add_argument("--state", default="collibra_user_email_state.json", help="Snapshot file used to detect changes across runs")
    parser.add_argument("--changelog", default="collibra_email_changelog.txt", help="Plain-text log appended to whenever email changes are detected")
    args = parser.parse_args()

    if not (args.base_url and args.username and args.password):
        sys.exit("Missing --base-url/--username/--password (or COLLIBRA_BASE_URL/COLLIBRA_USERNAME/COLLIBRA_PASSWORD env vars).")

    base_url = args.base_url.rstrip("/")

    session = authenticate(base_url, args.username, args.password)
    users = fetch_all_users(session, base_url)
    snapshot = build_snapshot(users)
    previous = load_previous_state(args.state)

    changed_count = write_report(args.output, snapshot, previous, changelog_path=args.changelog)

    with open(args.state, "w") as f:
        json.dump(snapshot, f, indent=2)

    print(f"\n{len(snapshot)} users written to {args.output}. {changed_count} email change(s) detected since last run "
          f"({datetime.now(timezone.utc).isoformat()}).")


if __name__ == "__main__":
    main()
