# Expense Tracker Bot

A private Telegram bot for tracking shared expenses between Princess and Jay. Supports custom split ratios, period-based settlement, category tagging, and scheduled budget alerts.

---

## Features

- **Expense tracking** — single or bulk entry with confirmation
- **Category tagging** — tag expenses with a category via keyboard picker or `#tag` inline syntax
- **Recurring expenses** — tag with `#recurring`, list with `/recurring`, and carry selected items into a new period after `/reset`
- **Custom splits** — `Princess 100 dinner -split 60/40` (any ratio, must sum to 100)
- **Smart settlement** — calculates who owes whom based on who paid vs. who owes per split
- **Period management** — close periods, view history, track settlements over time
- **Budget alerts** — automated Telegram messages via cron/systemd timer
- **Budget progress bar** — `/status` shows a visual ASCII bar with days remaining
- **Configurable** — set budget, split, and reminder frequency from within Telegram
- **Secure** — only registered chat IDs can interact with the bot; rate limiting enforced

---

## Quick Start

```bash
# 1. Copy / clone files and cd into the directory
cd ~/expense-tracker

# 2. Set up environment
cp .env.example .env
nano .env          # add TELEGRAM_BOT_TOKEN at minimum

# 3. Install (creates venv, installs deps, sets up systemd + cron)
bash install.sh

# 4. Enable and start
sudo systemctl enable expense-tracker-bot
sudo systemctl start expense-tracker-bot
```

---

## Expense Formats

```
# Single expense (50/50 split by default)
Princess 50 groceries
Jay 25.50 coffee

# Pre-tag a category inline
Princess 50 groceries #groceries
Jay 67 olive garden #dining

# Custom split (Princess owes 60%, Jay owes 40%)
Princess 100 dinner -split 60/40

# Personal expense (Jay pays and owes 100%)
Jay 75 car repair -split 0/100

# Bulk entry
Princess
- 342 groceries #groceries
- 67 olive garden -split 60/40

Jay
- 30 lunch
- 15.50 coffee
```

After sending a single expense, a **category keyboard** appears. Tap a category to save instantly. If you pre-tagged with `#category`, that category is pre-selected and the expense saves without prompting.

---

## Categories

Nine built-in categories are seeded on first run. Custom category names are also accepted.

| Emoji | Name        | `#tag` shortcut  |
|-------|-------------|------------------|
| 🛒    | Groceries   | `#groceries`     |
| 🍽    | Dining      | `#dining`        |
| ⛽    | Transport   | `#transport`     |
| 🏠    | Home        | `#home`          |
| 🎉    | Fun         | `#fun`           |
| 💊    | Health      | `#health`        |
| ✈️    | Travel      | `#travel`        |
| 📦    | Other       | `#other`         |
| 🔁    | Recurring   | `#recurring`     |

**Inline tagging:**
```
Jay 45 uber eats #dining
Princess 120 flight #travel -split 60/40
```

**Category picker (no pre-tag):**
Send `Jay 45 pizza` → a keyboard of category buttons appears → tap one → saved.

**Recurring expenses:** tag with `#recurring` to have them appear in `/recurring`.

---

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Register your chat ID and get a welcome message |
| `/status` | Current period totals, settlement, ASCII budget bar, and itemized breakdown |
| `/summary` | Category breakdown and spending summary for the current period |
| `/history` | Last 15 transactions |
| `/periods` | Period history (last 10) |
| `/edit` | Edit person, amount, description, or category of a transaction |
| `/undo` | Remove the most recent transaction immediately (no confirmation) |
| `/reset` | Close current period, auto-save CSV, record settlement, start fresh, then offer recurring carry-over |
| `/export` | Pick a period and download its transactions as a CSV |
| `/find [keyword]` | Search transactions by keyword, e.g. `/find safeway` |
| `/recurring` | List all expenses tagged `#recurring` |
| `/setbudget [amount]` | Change monthly budget, e.g. `/setbudget 800` |
| `/setreminder [days]` | Change alert frequency, 1–30 days |
| `/setsplit [P%/J%]` | Change default split ratio, e.g. `/setsplit 60/40` |
| `/reminder_help` | Shows the exact cron line to update |

---

## BotFather Command List

Paste this exactly into BotFather when prompted:

```
start - Register your chat ID and get started
status - Current period totals, budget bar, and breakdown
summary - Category breakdown for the current period
history - Last 15 transactions
periods - Period history (last 10)
edit - Edit a transaction (person, amount, description, or category)
undo - Remove the most recent transaction instantly
reset - Close current period, save CSV, and start fresh
export - Pick a period and download its CSV
find - Search transactions by keyword
recurring - List all recurring expenses
setbudget - Change monthly budget
setreminder - Change reminder alert frequency
setsplit - Change default expense split ratio (Princess%/Jay%)
reminder_help - Show the cron line to update for reminder schedule
```

---

## Database Schema

SQLite with WAL mode for safe concurrent access.

```
transactions  id, person, amount, description, timestamp,
              period_id, payer, split_ratio, category

periods       id, start_date, end_date, is_active,
              princess_total, jay_total, settlement_description

settings      key, value
              (keys: monthly_budget, princess_chat_id, jay_chat_id,
                     reminder_days, default_split)
```

`setup_database.py` is idempotent — run it after any schema change or fresh install. The `category` column is nullable; older rows without a category display as "Uncategorized".

---

## Files

```
expense_bot.py                 Main bot (production)
budget_alert.py                Cron/timer script for budget alerts
config.py                      Config loader (reads .env)
setup_database.py              Database initialisation and migration
install.sh                     Full install (venv + systemd + cron)
sudo-setup.sh                  System-only setup (run with sudo)
change-reminder-schedule.sh    Change systemd timer schedule interactively
data/exports/                  Auto-saved period CSVs (written on /reset)
tests/                         Unit test suite (pytest)
```

## Development

```bash
# Run tests
venv/bin/python -m pytest tests/ -v

# Initialize or repair the database schema
venv/bin/python setup_database.py

# Run the bot locally
venv/bin/python expense_bot.py
```

The test suite currently covers 241 cases across core bot logic and budget alert behavior.

---

## CSV Export Format

Exported CSVs (both manual `/export` and auto-saved on `/reset`) use this column order:

```
ID | Date | Payer | Amount | Description | Category | Split Ratio | Period ID
```

Auto-saved files land in `data/exports/` with the filename `period_<id>_<date>.csv`.
The `/export` command shows a period picker keyboard — select the current period or any previously saved period to download.

---

## Deploying to an Existing Device

The canonical install location is `/home/jevonx/expense-tracker/`. All code, the virtualenv, and the database live here. The systemd service and `.env` both point to this directory.

```bash
# Stop the bot
sudo systemctl stop expense-tracker-bot

# Backup the DB
cp data/expenses.db data/expenses-backup-$(date +%Y%m%d).db

# Copy updated files
scp expense_bot.py budget_alert.py setup_database.py \
    user@device:/home/jevonx/expense-tracker/

# Run migration (adds WAL mode, new columns if upgrading)
/home/jevonx/expense-tracker/venv/bin/python3 setup_database.py

# Restart
sudo systemctl start expense-tracker-bot
sudo systemctl status expense-tracker-bot
```

---

## Security Notes

- Bot token lives only in `.env` (chmod 600, never committed)
- Once both chat IDs are registered, the bot rejects all other senders (`is_authorized()`)
- All inputs validated and length-limited before touching the DB
- Rate limiting: 30 requests per 60-second sliding window per user
- SQLite foreign keys enforced; WAL mode prevents corruption on unclean shutdown
- Systemd unit runs with `NoNewPrivileges=yes` and `PrivateTmp=yes`
- Bulk inserts use a single DB transaction (atomic commit or full rollback)

---

## Changing the Alert Schedule

**Via Telegram (updates DB, then update cron manually):**
```
/setreminder 7
/reminder_help    # shows the exact cron line to paste
```

**Via systemd timer (if you used sudo-setup.sh):**
```bash
sudo bash change-reminder-schedule.sh
```

---

## Troubleshooting

```bash
# Bot not starting
sudo journalctl -u expense-tracker-bot -n 50

# Budget alerts not sending
python3 budget_alert.py   # run manually to see errors in stdout

# Check DB contents
sqlite3 data/expenses.db "SELECT * FROM settings;"
sqlite3 data/expenses.db "SELECT COUNT(*) FROM transactions;"

# Check category data
sqlite3 data/expenses.db "SELECT category, COUNT(*) FROM transactions GROUP BY category;"

# Reset DB (DESTRUCTIVE -- backup first!)
cp data/expenses.db data/expenses-backup-manual.db
rm data/expenses.db
python3 setup_database.py
```

---

**Last updated:** April 2026 | v4.0
