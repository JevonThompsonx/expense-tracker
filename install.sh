#!/usr/bin/env bash
# ==============================================================================
# install.sh - Install and configure the Expense Tracker Bot
#
# Installs the bot as a systemd service with a cron job for budget alerts.
# Run as a regular user; uses sudo only for system-level operations.
#
# Usage:
#   bash install.sh
#
# Dependencies:
#   python3, python3-venv, systemd, crontab
# ==============================================================================
set -euo pipefail

# --- Configuration ---
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly INSTALL_DIR="${HOME}/expense-tracker"
readonly VENV_DIR="${INSTALL_DIR}/venv"
readonly SERVICE_NAME="expense-tracker-bot"
readonly SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
readonly LOG_FILE="/var/log/expense-tracker-budget.log"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 2; }

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

if [[ "${EUID}" -eq 0 ]]; then
    fail "Do not run as root. Run as a regular user (sudo will be used as needed)."
fi

for cmd in python3 pip3 sudo systemctl crontab; do
    if ! command -v "${cmd}" &>/dev/null; then
        fail "Required command not found: ${cmd}"
    fi
done

echo "======================================"
echo " Expense Tracker Bot - Installation"
echo "======================================"
echo ""

# ---------------------------------------------------------------------------
# Python version check
# ---------------------------------------------------------------------------

python_version="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
ok "Python ${python_version} found"

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------

sudo mkdir -p /var/lib/expense-tracker
sudo chown "${USER}:${USER}" /var/lib/expense-tracker
ok "Created /var/lib/expense-tracker"

mkdir -p "${INSTALL_DIR}"
ok "Install directory: ${INSTALL_DIR}"

# Copy project files if running from a different directory
if [[ "${SCRIPT_DIR}" != "${INSTALL_DIR}" ]]; then
    cp -r "${SCRIPT_DIR}/." "${INSTALL_DIR}/"
    ok "Copied project files to ${INSTALL_DIR}"
fi

# ---------------------------------------------------------------------------
# Virtual environment + dependencies
# ---------------------------------------------------------------------------

if [[ ! -d "${VENV_DIR}" ]]; then
    python3 -m venv "${VENV_DIR}"
    ok "Created virtual environment: ${VENV_DIR}"
else
    ok "Virtual environment already exists: ${VENV_DIR}"
fi

"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
ok "Python dependencies installed"

# ---------------------------------------------------------------------------
# Environment file
# ---------------------------------------------------------------------------

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
    if [[ -f "${INSTALL_DIR}/.env.example" ]]; then
        cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
        chmod 600 "${INSTALL_DIR}/.env"
        warn ".env created from .env.example -- edit it and add your TELEGRAM_BOT_TOKEN"
    else
        warn "No .env file found. Create ${INSTALL_DIR}/.env with your bot token."
    fi
else
    ok ".env already exists"
fi

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

"${VENV_DIR}/bin/python3" "${INSTALL_DIR}/setup_database.py"
ok "Database initialized"

# ---------------------------------------------------------------------------
# Systemd service
# ---------------------------------------------------------------------------

sudo tee "${SERVICE_FILE}" > /dev/null <<EOF
[Unit]
Description=Expense Tracker Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${INSTALL_DIR}
Environment="PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=${VENV_DIR}/bin/python3 ${INSTALL_DIR}/expense_bot.py
Restart=on-failure
RestartSec=10

# Basic hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}/data /var/lib/expense-tracker

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
ok "Systemd service installed: ${SERVICE_FILE}"

# ---------------------------------------------------------------------------
# Log file
# ---------------------------------------------------------------------------

sudo touch "${LOG_FILE}"
sudo chown "${USER}:${USER}" "${LOG_FILE}"
ok "Log file: ${LOG_FILE}"

# ---------------------------------------------------------------------------
# Cron job for budget alerts (every 3 days at 9 AM)
# ---------------------------------------------------------------------------

CRON_JOB="0 9 */3 * * cd ${INSTALL_DIR} && ${VENV_DIR}/bin/python3 ${INSTALL_DIR}/budget_alert.py >> ${LOG_FILE} 2>&1"

if crontab -l 2>/dev/null | grep -qF "budget_alert.py"; then
    ok "Cron job already exists"
else
    (crontab -l 2>/dev/null || true; echo "${CRON_JOB}") | crontab -
    ok "Cron job added (every 3 days at 09:00)"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "======================================"
ok "Installation complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo ""
echo "  1. Edit your bot token:"
echo "     nano ${INSTALL_DIR}/.env"
echo ""
echo "  2. Enable and start the bot:"
echo "     sudo systemctl enable ${SERVICE_NAME}"
echo "     sudo systemctl start ${SERVICE_NAME}"
echo ""
echo "  3. Check status:"
echo "     sudo systemctl status ${SERVICE_NAME}"
echo ""
echo "  4. View logs:"
echo "     sudo journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "  5. Have Princess and Jay send /start to register their chat IDs"
echo ""
