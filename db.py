"""SQLite storage. All money is stored as integer cents."""
import os
import sqlite3

DB_PATH = os.environ.get("BUDGET_DB", os.path.join(os.path.dirname(__file__), "data", "budget.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

-- Templates copied into each new month (kind = 'income' | 'fixed')
CREATE TABLE IF NOT EXISTS recurring (
    id     INTEGER PRIMARY KEY,
    kind   TEXT NOT NULL CHECK (kind IN ('income', 'fixed')),
    name   TEXT NOT NULL,
    amount INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS months (
    month      TEXT PRIMARY KEY,          -- 'YYYY-MM'
    savings    INTEGER NOT NULL DEFAULT 0,
    reflection TEXT NOT NULL DEFAULT ''
);

-- Per-month expected income and fixed expenses
CREATE TABLE IF NOT EXISTS month_items (
    id     INTEGER PRIMARY KEY,
    month  TEXT NOT NULL REFERENCES months(month),
    kind   TEXT NOT NULL CHECK (kind IN ('income', 'fixed')),
    name   TEXT NOT NULL,
    amount INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    external_id TEXT UNIQUE,
    name        TEXT NOT NULL,
    org         TEXT NOT NULL DEFAULT '',
    balance     INTEGER,
    balance_at  TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1
);

-- amount < 0 is money out, amount > 0 is money in.
-- excluded = 1 means it doesn't count toward spending (paychecks, transfers, card payments).
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY,
    external_id TEXT UNIQUE,
    account_id  INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    date        TEXT NOT NULL,            -- 'YYYY-MM-DD'
    description TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    excluded    INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'manual'   -- manual | simplefin | csv
);
CREATE INDEX IF NOT EXISTS tx_date ON transactions(date);
"""

# The four traditional Kakeibo pillars, seeded on first run. Rename/delete freely.
DEFAULT_CATEGORIES = ["Needs", "Wants", "Culture", "Unexpected"]


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init():
    with connect() as conn:
        fresh = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'categories'"
        ).fetchone() is None
        conn.executescript(SCHEMA)
        if fresh:
            conn.executemany(
                "INSERT INTO categories (name) VALUES (?)",
                [(c,) for c in DEFAULT_CATEGORIES],
            )


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def ensure_month(conn, month):
    """Create the month on first visit, seeding it from recurring templates."""
    if conn.execute("SELECT 1 FROM months WHERE month = ?", (month,)).fetchone():
        return
    # Carry the savings goal forward from the most recent prior month.
    prev = conn.execute(
        "SELECT savings FROM months WHERE month < ? ORDER BY month DESC LIMIT 1", (month,)
    ).fetchone()
    conn.execute(
        "INSERT INTO months (month, savings) VALUES (?, ?)",
        (month, prev["savings"] if prev else 0),
    )
    conn.execute(
        "INSERT INTO month_items (month, kind, name, amount) "
        "SELECT ?, kind, name, amount FROM recurring",
        (month,),
    )


def upsert_transaction(conn, external_id, account_id, date, description, amount, source):
    """Insert an imported transaction; on re-sync update facts but keep user's category/exclusion.

    New money-in transactions default to excluded, since paychecks are already
    counted as expected income. Un-exclude refunds manually.
    """
    conn.execute(
        """
        INSERT INTO transactions
            (external_id, account_id, date, description, amount, excluded, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(external_id) DO UPDATE SET
            date = excluded.date,
            description = excluded.description,
            amount = excluded.amount
        """,
        (external_id, account_id, date, description, amount, 1 if amount > 0 else 0, source),
    )
