# Kakeibo Budget

A small personal budgeting app built around the Kakeibo method:

1. **Expected income**: what's coming in this month
2. **Fixed expenses**: rent, utilities, subscriptions
3. **Savings goal**: set aside first
4. **Transactions**: subtracted from what's left, sorted into your own categories

Bank transactions sync automatically through [SimpleFIN Bridge](https://bridge.simplefin.org) and arrive **uncategorized**, so you sort them yourself.

## Quick start

```sh
./run.sh            # first run creates .venv and installs Flask
```

Then open http://127.0.0.1:5000.

1. **Settings → Monthly templates**: add your paycheck(s) and fixed bills. Every new month starts with these. You can also tick "monthly" when adding items on a month page.
2. **Month page → Savings goal**: set it once. It carries forward to later months.
3. **Settings → Categories**: it starts with the four traditional Kakeibo pillars (Needs, Wants, Culture, Unexpected). Rename, delete, or add your own.
4. **Connect your bank** (below), or import CSVs, or add transactions by hand.

## Bank connection (SimpleFIN)

SimpleFIN Bridge costs about $15/year. It connects to most US banks through MX and gives you read-only access without a developer account.

1. Sign up at https://bridge.simplefin.org and link your bank accounts there.
2. Create a **New connection** to get a setup token.
3. In the app, go to **Settings → Bank connection**, paste the token, and click Connect.

While the app is running it syncs every 6 hours. You can also click **Sync now**, or run a sync from cron:

```sh
0 */6 * * * /path/to/BasicBudgetingAppV1/run.sh sync
```

Avoiding double-counting:

- **Money coming in** (paychecks, refunds) is imported as *not counted*, because income is already planned in step 1. Click **Count** on refunds so they reduce your spending.
- **Transfers and credit-card payments**: click **Ignore** on them. If you track the card itself, its purchases already count.
- **Pending transactions** are skipped until they post.

## CSV import

For any bank SimpleFIN doesn't cover: **Settings → Import CSV**. The app detects the Date, Description, and Amount columns (or Debit/Credit columns) on its own. You can re-import overlapping files and nothing is duplicated.

## Data and security

- Everything is stored in `data/budget.db` (SQLite). Back up that one file.
- The SimpleFIN access URL, which is a read-only credential, is stored in that database. Keep the file private.
- The app only listens on `127.0.0.1` and has **no login**. Don't expose it to a network without putting authentication in front of it. To reach it from your phone, use something like Tailscale.

Environment variables: `BUDGET_DB`, `BUDGET_HOST`, `BUDGET_PORT`, `BUDGET_SECRET`.

## Layout

```
app.py         Flask routes + Kakeibo math + background sync
db.py          SQLite schema and helpers (money stored as integer cents)
importers.py   SimpleFIN client and CSV importer
templates/     Server-rendered pages (month view, settings)
static/        CSS
```
