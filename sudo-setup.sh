#!/usr/bin/env bash
# ==============================================================================
# sudo-setup.sh - System-level setup for Expense Tracker Bot
#
# Installs the systemd service and cron job. Must run with sudo or as root.
# Assumes the bot files are already in SCRIPT_DIR and a venv exists there.
#
# Usage:
#   sudo bash sudo-setup.sh
# ==============================================================================
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SERVICE_NAME="expense-tracker-bot"
readonly SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
readonly VENV_PYTHON="${SCRIPT_DIR}/venv/bin/python3"
readonly LOG_FILE="${SCRIPT_DIR}/data/budget-alert.log"

# --- Pre-flight ---

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run with sudo: sudo bash $0" >&2
    exit 2
fi

if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "Virtual environment not found at ${SCRIPT_DIR}/venv" >&2
    echo "Run install.sh first." >&2
    exit 2
fi

# Install cronie if crontab is missing (Arch Linux)
if ! command -v crontab &>/dev/null; then
    if command -v pacman &>/dev/null; then
        pacman -S --noconfirm cronie
        systemctl enable --now cronie
        echo "[OK] Installed and started cronie"
    else
        echo "[FAIL] crontab not found and pacman not available" >&2
        exit 2
    fi
fi

# --- Systemd service ---

# Determine the non-root owner of the script directory
dir_owner="$(stat -c '%U' "${SCRIPT_DIR}")"

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Expense Tracker Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${dir_owner}
WorkingDirectory=${SCRIPT_DIR}
Environment="PATH=${SCRIPT_DIR}/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=${VENV_PYTHON} ${SCRIPT_DIR}/expense_bot.py
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
echo "[OK] Service ${SERVICE_NAME} installed and started"

# --- Data directory and log file ---

mkdir -p "${SCRIPT_DIR}/data"
chown "${dir_owner}:${dir_owner}" "${SCRIPT_DIR}/data"
touch "${LOG_FILE}"
chown "${dir_owner}:${dir_owner}" "${LOG_FILE}"
echo "[OK] Data directory and log file ready"

# --- Cron job for budget alerts (every 3 days at 09:00) ---

CRON_JOB="0 9 */3 * * ${VENV_PYTHON} ${SCRIPT_DIR}/budget_alert.py >> ${LOG_FILE} 2>&1"

if crontab -u "${dir_owner}" -l 2>/dev/null | grep -qF "budget_alert.py"; then
    echo "[OK] Cron job already exists"
else
    (crontab -u "${dir_owner}" -l 2>/dev/null || true; echo "${CRON_JOB}") \
        | crontab -u "${dir_owner}" -
    echo "[OK] Cron job added (every 3 days at 09:00)"
fi

echo ""
echo "[OK] Setup complete!"
echo ""
echo "Check status:  sudo systemctl status ${SERVICE_NAME}"
echo "View logs:     sudo journalctl -u ${SERVICE_NAME} -f"
