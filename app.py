"""Kakeibo budget app.

Run:  python app.py            (serves http://127.0.0.1:5000)
      python app.py sync       (pull bank transactions once, e.g. from cron)
"""
import calendar
import json
import os
import sys
import threading
from datetime import date, datetime

from flask import Flask, flash, redirect, render_template, request, url_for

import db
import importers
from importers import ImportFailed, to_cents

app = Flask(__name__)
app.secret_key = os.environ.get("BUDGET_SECRET", "local-only-budget-app")

SYNC_INTERVAL_HOURS = 6  # SimpleFIN allows ~24 requests/day


@app.template_filter("money")
def money(cents):
    cents = cents or 0
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


@app.template_filter("dollars")
def dollars(cents):
    """Plain number for pre-filling inputs."""
    return f"{(cents or 0) / 100:.2f}"


def back(anchor=""):
    return redirect((request.referrer or url_for("index")).split("#")[0] + anchor)


def shift_month(month, delta):
    y, m = map(int, month.split("-"))
    m += delta
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def valid_month(month):
    try:
        y, m = map(int, month.split("-"))
        return len(month) == 7 and 1 <= m <= 12
    except ValueError:
        return False


# ---------------------------------------------------------------- month view

@app.route("/")
def index():
    return redirect(url_for("month_view", month=date.today().strftime("%Y-%m")))


@app.route("/month/<month>")
def month_view(month):
    if not valid_month(month):
        return redirect(url_for("index"))
    show = request.args.get("show", "all")  # all | uncategorized | excluded | <category id>

    with db.connect() as conn:
        db.ensure_month(conn, month)
        mrow = conn.execute("SELECT * FROM months WHERE month = ?", (month,)).fetchone()
        items = conn.execute(
            "SELECT * FROM month_items WHERE month = ? ORDER BY amount DESC", (month,)
        ).fetchall()
        categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()

        like = month + "-%"
        all_tx = conn.execute(
            """SELECT t.*, a.name AS account_name, c.name AS category_name
               FROM transactions t
               LEFT JOIN accounts a ON a.id = t.account_id
               LEFT JOIN categories c ON c.id = t.category_id
               WHERE t.date LIKE ? ORDER BY t.date DESC, t.id DESC""",
            (like,),
        ).fetchall()
        by_cat = conn.execute(
            """SELECT c.id, COALESCE(c.name, 'Uncategorized') AS name, -SUM(t.amount) AS spent,
                      COUNT(*) AS n
               FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
               WHERE t.date LIKE ? AND t.excluded = 0
               GROUP BY c.id ORDER BY spent DESC""",
            (like,),
        ).fetchall()
        last_sync = db.get_setting(conn, "last_sync")
        connected = bool(db.get_setting(conn, "simplefin_access_url"))

    income = [i for i in items if i["kind"] == "income"]
    fixed = [i for i in items if i["kind"] == "fixed"]
    total_income = sum(i["amount"] for i in income)
    total_fixed = sum(i["amount"] for i in fixed)
    savings = mrow["savings"]
    available = total_income - total_fixed - savings
    spent = -sum(t["amount"] for t in all_tx if not t["excluded"])
    remaining = available - spent

    # Daily allowance for a given day = money left at the *start* of that day spread over
    # the days left (that day included). Using start-of-day means spending today lowers
    # what's "left today" without also shrinking today's allowance.
    today = date.today()
    this_month = today.strftime("%Y-%m")
    y, m = map(int, month.split("-"))
    days_in_month = calendar.monthrange(y, m)[1]
    planned = available // days_in_month if available > 0 else 0  # even pace across the month

    spent_by_day = {}
    for t in all_tx:
        if not t["excluded"]:
            d = int(t["date"][8:10])
            spent_by_day[d] = spent_by_day.get(d, 0) - t["amount"]

    # Day-by-day breakdown: through today for this month, whole month for past months.
    last_day = today.day if month == this_month else (days_in_month if month < this_month else 0)
    days = []
    spent_before = 0
    for d in range(1, last_day + 1):
        allowance = max(available - spent_before, 0) // (days_in_month - d + 1)
        day_spent = spent_by_day.get(d, 0)
        days.append({
            "date": date(y, m, d),
            "allowance": allowance,
            "spent": day_spent,
            "left": allowance - day_spent,
            "is_today": month == this_month and d == today.day,
        })
        spent_before += day_spent

    # Headline: today's numbers for this month, a planning figure for future months.
    per_day = None
    if month == this_month:
        t = days[-1]
        per_day = {
            "amount": t["allowance"],
            "spent_today": t["spent"],
            "left_today": t["left"],
            "days_left": days_in_month - today.day + 1,
            "planned": planned,
            "status": "over" if remaining < 0 else
                      "behind" if t["left"] < 0 or t["allowance"] < planned else "ahead",
        }
    elif month > this_month:
        per_day = {"amount": max(remaining, 0) // days_in_month, "days_left": days_in_month,
                   "planned": planned, "status": "over" if remaining < 0 else "ahead"}

    if show == "uncategorized":
        txs = [t for t in all_tx if t["category_id"] is None and not t["excluded"]]
    elif show == "excluded":
        txs = [t for t in all_tx if t["excluded"]]
    elif show.isdigit():
        txs = [t for t in all_tx if t["category_id"] == int(show) and not t["excluded"]]
    else:
        txs = all_tx
    uncategorized = sum(1 for t in all_tx if t["category_id"] is None and not t["excluded"])

    return render_template(
        "month.html",
        month=month,
        month_label=date(*map(int, month.split("-")), 1).strftime("%B %Y"),
        prev_month=shift_month(month, -1),
        next_month=shift_month(month, 1),
        income=income, fixed=fixed, total_income=total_income, total_fixed=total_fixed,
        savings=savings, available=available, spent=spent, remaining=remaining,
        per_day=per_day, days=days, reflection=mrow["reflection"],
        txs=txs, show=show, by_cat=by_cat, uncategorized=uncategorized,
        categories=categories, today=today.isoformat(),
        last_sync=last_sync, connected=connected,
    )


@app.post("/month/<month>/item")
def add_item(month):
    name = request.form["name"].strip()
    if name:
        with db.connect() as conn:
            db.ensure_month(conn, month)
            conn.execute(
                "INSERT INTO month_items (month, kind, name, amount) VALUES (?, ?, ?, ?)",
                (month, request.form["kind"], name, abs(to_cents(request.form["amount"]))),
            )
            if request.form.get("recurring"):
                conn.execute("INSERT INTO recurring (kind, name, amount) VALUES (?, ?, ?)",
                             (request.form["kind"], name, abs(to_cents(request.form["amount"]))))
    return back("#" + request.form["kind"])


@app.post("/item/<int:item_id>/update")
def update_item(item_id):
    with db.connect() as conn:
        conn.execute("UPDATE month_items SET amount = ? WHERE id = ?",
                     (abs(to_cents(request.form["amount"])), item_id))
    return back()


@app.post("/item/<int:item_id>/delete")
def delete_item(item_id):
    with db.connect() as conn:
        conn.execute("DELETE FROM month_items WHERE id = ?", (item_id,))
    return back()


@app.post("/month/<month>/savings")
def set_savings(month):
    with db.connect() as conn:
        db.ensure_month(conn, month)
        conn.execute("UPDATE months SET savings = ? WHERE month = ?",
                     (abs(to_cents(request.form["amount"])), month))
    return back("#savings")


@app.post("/month/<month>/reflection")
def set_reflection(month):
    with db.connect() as conn:
        db.ensure_month(conn, month)
        conn.execute("UPDATE months SET reflection = ? WHERE month = ?",
                     (request.form["reflection"], month))
    flash("Reflection saved.")
    return back("#reflection")


# ---------------------------------------------------------------- transactions

@app.post("/tx/add")
def add_tx():
    amount = abs(to_cents(request.form["amount"]))
    if request.form.get("direction") != "in":
        amount = -amount
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO transactions (date, description, amount, category_id) VALUES (?, ?, ?, ?)",
            (request.form["date"], request.form["description"].strip() or "(manual)",
             amount, request.form.get("category_id") or None),
        )
    return back("#transactions")


@app.post("/tx/<int:tx_id>/update")
def update_tx(tx_id):
    with db.connect() as conn:
        if "category_id" in request.form:
            conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?",
                         (request.form["category_id"] or None, tx_id))
        if "excluded" in request.form:
            conn.execute("UPDATE transactions SET excluded = ? WHERE id = ?",
                         (int(request.form["excluded"]), tx_id))
    return back(f"#tx-{tx_id}")


@app.post("/tx/<int:tx_id>/delete")
def delete_tx(tx_id):
    with db.connect() as conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    return back("#transactions")


# ---------------------------------------------------------------- settings

@app.route("/settings")
def settings():
    with db.connect() as conn:
        categories = conn.execute(
            """SELECT c.*, COUNT(t.id) AS n FROM categories c
               LEFT JOIN transactions t ON t.category_id = c.id
               GROUP BY c.id ORDER BY c.name"""
        ).fetchall()
        recurring = conn.execute("SELECT * FROM recurring ORDER BY kind DESC, amount DESC").fetchall()
        accounts = conn.execute("SELECT * FROM accounts ORDER BY org, name").fetchall()
        connected = bool(db.get_setting(conn, "simplefin_access_url"))
        last_sync = db.get_setting(conn, "last_sync")
        sync_messages = json.loads(db.get_setting(conn, "last_sync_messages", "[]"))
    return render_template("settings.html", categories=categories, recurring=recurring,
                           accounts=accounts, connected=connected, last_sync=last_sync,
                           sync_messages=sync_messages)


@app.post("/categories/add")
def add_category():
    name = request.form["name"].strip()
    if name:
        with db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    return back()


@app.post("/categories/<int:cat_id>/rename")
def rename_category(cat_id):
    name = request.form["name"].strip()
    if name:
        with db.connect() as conn:
            try:
                conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, cat_id))
            except Exception:
                flash(f"A category named {name!r} already exists.")
    return back()


@app.post("/categories/<int:cat_id>/delete")
def delete_category(cat_id):
    with db.connect() as conn:
        conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    return back()


@app.post("/recurring/add")
def add_recurring():
    name = request.form["name"].strip()
    if name:
        with db.connect() as conn:
            conn.execute("INSERT INTO recurring (kind, name, amount) VALUES (?, ?, ?)",
                         (request.form["kind"], name, abs(to_cents(request.form["amount"]))))
    return back()


@app.post("/recurring/<int:rec_id>/delete")
def delete_recurring(rec_id):
    with db.connect() as conn:
        conn.execute("DELETE FROM recurring WHERE id = ?", (rec_id,))
    return back()


@app.post("/simplefin/connect")
def simplefin_connect():
    try:
        access_url = importers.claim_setup_token(request.form["token"])
        with db.connect() as conn:
            db.set_setting(conn, "simplefin_access_url", access_url)
        new, msgs = importers.sync_simplefin(days=int(request.form.get("days") or 60))
        flash(f"Connected! Imported {new} transactions.")
        for m in msgs:
            flash(f"Bank message: {m}")
    except ImportFailed as e:
        flash(str(e))
    return back()


@app.post("/simplefin/disconnect")
def simplefin_disconnect():
    with db.connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = 'simplefin_access_url'")
    flash("Disconnected. Existing transactions were kept.")
    return back()


@app.post("/sync")
def sync_now():
    try:
        new, msgs = importers.sync_simplefin()
        flash(f"Synced: {new} new transaction{'s' if new != 1 else ''}.")
        for m in msgs:
            flash(f"Bank message: {m}")
    except ImportFailed as e:
        flash(str(e))
    except Exception as e:  # network errors etc.
        flash(f"Sync failed: {e}")
    return back()


@app.post("/accounts/<int:acct_id>/toggle")
def toggle_account(acct_id):
    with db.connect() as conn:
        conn.execute("UPDATE accounts SET enabled = 1 - enabled WHERE id = ?", (acct_id,))
    return back()


@app.post("/accounts/<int:acct_id>/delete")
def delete_account(acct_id):
    with db.connect() as conn:
        conn.execute("DELETE FROM transactions WHERE account_id = ?", (acct_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (acct_id,))
    flash("Account and its transactions removed.")
    return back()


@app.post("/csv/import")
def csv_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a CSV file first.")
        return back()
    try:
        new = importers.import_csv(f.read(), request.form["account"].strip() or "CSV import",
                                   flip_sign=bool(request.form.get("flip")))
        flash(f"Imported {new} new transactions from {f.filename}.")
    except ImportFailed as e:
        flash(str(e))
    except ValueError as e:
        flash(f"Couldn't parse an amount in that file: {e}")
    return back()


# ---------------------------------------------------------------- background sync

def _background_sync():
    try:
        with db.connect() as conn:
            last = db.get_setting(conn, "last_sync")
        stale = not last or (datetime.now() - datetime.fromisoformat(last)).total_seconds() > SYNC_INTERVAL_HOURS * 3600 - 60
        if stale:  # avoid burning SimpleFIN's daily quota on app restarts
            importers.sync_simplefin()
    except Exception as e:
        print(f"[sync] {e}", file=sys.stderr)
    t = threading.Timer(SYNC_INTERVAL_HOURS * 3600, _background_sync)
    t.daemon = True
    t.start()


db.init()

if __name__ == "__main__":
    if sys.argv[1:] == ["sync"]:
        new, msgs = importers.sync_simplefin()
        print(f"Imported {new} new transactions.", *msgs, sep="\n")
        sys.exit()
    with db.connect() as conn:
        if db.get_setting(conn, "simplefin_access_url"):
            threading.Thread(target=_background_sync, daemon=True).start()
    host = os.environ.get("BUDGET_HOST", "127.0.0.1")
    port = int(os.environ.get("BUDGET_PORT", 5000))
    print(f"Budget app running at http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
