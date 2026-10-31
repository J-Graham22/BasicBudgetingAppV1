"""Bank data sources: SimpleFIN Bridge (automatic) and CSV files (manual fallback).

SimpleFIN protocol: https://www.simplefin.org/protocol.html
  1. User gets a one-time "setup token" from https://bridge.simplefin.org (base64 of a claim URL).
  2. POST to the claim URL -> returns an access URL with embedded credentials.
  3. GET {access_url}/accounts?start-date=... -> accounts + transactions as JSON.
"""
import base64
import csv
import hashlib
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import db


class ImportFailed(Exception):
    pass


def to_cents(value):
    s = str(value).strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")  # accounting style (12.34)
    s = s.strip("()")
    if not s:
        return 0
    cents = round(float(s) * 100)
    return -cents if neg else cents


# ---------------------------------------------------------------- SimpleFIN

def claim_setup_token(setup_token):
    """Exchange a one-time setup token for a long-lived access URL."""
    try:
        claim_url = base64.b64decode(setup_token.strip()).decode()
    except Exception:
        raise ImportFailed("That doesn't look like a SimpleFIN setup token.")
    if not claim_url.startswith("https://"):
        raise ImportFailed("That doesn't look like a SimpleFIN setup token.")
    req = urllib.request.Request(claim_url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode().strip()
    except urllib.error.HTTPError as e:
        raise ImportFailed(f"Claim failed (HTTP {e.code}). Setup tokens can only be used once — generate a new one.")


def _fetch_accounts(access_url, start_ts):
    parts = urllib.parse.urlsplit(access_url)
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    url = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/") + "/accounts",
                                   urllib.parse.urlencode({"start-date": int(start_ts)}), ""))
    auth = base64.b64encode(
        f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password)}".encode()
    ).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise ImportFailed("SimpleFIN rejected the credentials. Reconnect in Settings.")
        raise ImportFailed(f"SimpleFIN request failed (HTTP {e.code}).")


def sync_simplefin(days=60):
    """Pull recent transactions from all connected banks. Returns (new_count, messages)."""
    with db.connect() as conn:
        access_url = db.get_setting(conn, "simplefin_access_url")
    if not access_url:
        raise ImportFailed("No bank connection configured.")

    data = _fetch_accounts(access_url, time.time() - days * 86400)
    messages = list(data.get("errors") or [])  # e.g. "please re-authenticate with your bank"
    new = 0
    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        for acct in data.get("accounts", []):
            org = (acct.get("org") or {}).get("name") or (acct.get("org") or {}).get("domain") or ""
            conn.execute(
                """INSERT INTO accounts (external_id, name, org, balance, balance_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(external_id) DO UPDATE SET
                     name = excluded.name, org = excluded.org,
                     balance = excluded.balance, balance_at = excluded.balance_at""",
                ("sf:" + acct["id"], acct.get("name") or "Account", org,
                 to_cents(acct.get("balance", 0)),
                 datetime.fromtimestamp(acct.get("balance-date", time.time()), timezone.utc).isoformat()),
            )
            row = conn.execute("SELECT id, enabled FROM accounts WHERE external_id = ?",
                               ("sf:" + acct["id"],)).fetchone()
            if not row["enabled"]:
                continue
            for tx in acct.get("transactions", []):
                if tx.get("pending"):
                    continue  # pending IDs change once posted; wait for the posted version
                ts = tx.get("transacted_at") or tx.get("posted")
                date = datetime.fromtimestamp(ts).date().isoformat()
                desc = tx.get("payee") or tx.get("description") or tx.get("memo") or "(no description)"
                db.upsert_transaction(conn, f"sf:{acct['id']}:{tx['id']}", row["id"], date,
                                      desc.strip(), to_cents(tx["amount"]), "simplefin")
        new = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] - before
        db.set_setting(conn, "last_sync", datetime.now().isoformat(sep=" ", timespec="minutes"))
        db.set_setting(conn, "last_sync_messages", json.dumps(messages))
    return new, messages


# ---------------------------------------------------------------- CSV

def _find(headers, *patterns):
    for p in patterns:
        for i, h in enumerate(headers):
            if re.search(p, h, re.I):
                return i
    return None


def _parse_date(s):
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    raise ImportFailed(f"Unrecognized date format: {s!r}")


def import_csv(file_bytes, account_name, flip_sign=False):
    """Import a bank CSV export. Detects Date / Description / Amount (or Debit+Credit) columns.

    Re-importing the same file won't create duplicates.
    flip_sign: some card exports show purchases as positive numbers; tick this to invert.
    """
    text = file_bytes.decode("utf-8-sig", errors="replace")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ImportFailed("CSV appears empty.")
    headers = rows[0]
    i_date = _find(headers, r"^(transaction |posted |post )?date$", r"date")
    i_desc = _find(headers, r"description", r"payee", r"merchant", r"name", r"memo")
    i_amt = _find(headers, r"^amount$", r"amount")
    i_debit = _find(headers, r"debit", r"withdrawal")
    i_credit = _find(headers, r"credit", r"deposit")
    if i_date is None or i_desc is None or (i_amt is None and i_debit is None):
        raise ImportFailed(f"Couldn't find date/description/amount columns in: {headers}")

    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (external_id, name, org) VALUES (?, ?, 'CSV')",
                     ("csv:" + account_name, account_name))
        account_id = conn.execute("SELECT id FROM accounts WHERE external_id = ?",
                                  ("csv:" + account_name,)).fetchone()["id"]
        before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        seen = {}
        for r in rows[1:]:
            r += [""] * (len(headers) - len(r))
            if i_amt is not None:
                amount = to_cents(r[i_amt])
            else:
                amount = -abs(to_cents(r[i_debit])) + (abs(to_cents(r[i_credit])) if i_credit is not None else 0)
            if flip_sign:
                amount = -amount
            date = _parse_date(r[i_date])
            desc = r[i_desc].strip() or "(no description)"
            # Same date/desc/amount can legitimately repeat; number the occurrences.
            key = (date, desc, amount)
            seen[key] = seen.get(key, 0) + 1
            ext = "csv:" + hashlib.sha1(f"{account_name}|{date}|{desc}|{amount}|{seen[key]}".encode()).hexdigest()
            db.upsert_transaction(conn, ext, account_id, date, desc, amount, "csv")
        return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] - before
