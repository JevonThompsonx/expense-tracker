#!/usr/bin/env bash
# ==============================================================================
# change-reminder-schedule.sh - Update the budget alert systemd timer schedule
#
# Interactively changes the OnCalendar value of the expense-budget-alert.timer
# unit. Requires root (sudo) because it modifies a system timer file.
#
# Usage:
#   sudo bash change-reminder-schedule.sh
# ==============================================================================
set -euo pipefail

readonly TIMER_FILE="/etc/systemd/system/expense-budget-alert.timer"
readonly TIMER_NAME="expense-budget-alert.timer"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 2; }

# --- Pre-flight ---

if [[ "${EUID}" -ne 0 ]]; then
    fail "Run with sudo: sudo bash $0"
fi

echo -e "${BLUE}================================${NC}"
echo -e "${BLUE}Expense Budget Alert - Schedule${NC}"
echo -e "${BLUE}================================${NC}"
echo ""

if [[ -f "${TIMER_FILE}" ]]; then
    current_schedule="$(grep 'OnCalendar=' "${TIMER_FILE}" | cut -d'=' -f2)"
    echo -e "${GREEN}Current schedule:${NC} ${current_schedule}"
    next_run="$(systemctl list-timers "${TIMER_NAME}" --no-pager 2>/dev/null \
        | grep "${TIMER_NAME}" | awk '{print $1, $2, $3}' || true)"
    if [[ -n "${next_run}" ]]; then
        echo -e "${GREEN}Next run:${NC} ${next_run}"
    fi
else
    warn "Timer file not found: ${TIMER_FILE}"
fi

echo ""

# --- Frequency selection ---

echo "Select reminder frequency:"
echo ""
echo "  1) Weekly"
echo "  2) Biweekly (1st and 15th)"
echo "  3) Monthly"
echo "  4) Daily"
echo "  5) Custom (advanced)"
echo "  6) Cancel"
echo ""
read -rp "Choice [1-6]: " frequency_choice

case "${frequency_choice}" in
    1) frequency="weekly"   ;;
    2) frequency="biweekly" ;;
    3) frequency="monthly"  ;;
    4) frequency="daily"    ;;
    5) frequency="custom"   ;;
    6) echo "Cancelled."; exit 0 ;;
    *) fail "Invalid choice: ${frequency_choice}" ;;
esac

# --- Day of week (weekly / biweekly) ---

if [[ "${frequency}" == "weekly" || "${frequency}" == "biweekly" ]]; then
    echo ""
    echo "Select day of week:"
    echo "  1) Monday  2) Tuesday  3) Wednesday  4) Thursday"
    echo "  5) Friday  6) Saturday  7) Sunday"
    echo ""
    read -rp "Choice [1-7]: " day_choice
    case "${day_choice}" in
        1) day="Mon" ;; 2) day="Tue" ;; 3) day="Wed" ;; 4) day="Thu" ;;
        5) day="Fri" ;; 6) day="Sat" ;; 7) day="Sun" ;;
        *) fail "Invalid day choice: ${day_choice}" ;;
    esac
fi

# --- Day of month (monthly) ---

if [[ "${frequency}" == "monthly" ]]; then
    echo ""
    read -rp "Day of month (1-28): " month_day
    if ! [[ "${month_day}" =~ ^[0-9]+$ ]] || \
       [[ "${month_day}" -lt 1 ]] || [[ "${month_day}" -gt 28 ]]; then
        fail "Invalid day: ${month_day}. Use 1-28 to avoid month-end issues."
    fi
fi

# --- Time (all except custom) ---

if [[ "${frequency}" != "custom" ]]; then
    echo ""
    read -rp "Time (HH:MM, 24-hour): " time_input
    if ! [[ "${time_input}" =~ ^[0-9]{1,2}:[0-9]{2}$ ]]; then
        fail "Invalid time format. Use HH:MM (e.g. 09:00)"
    fi
    hour="${time_input%%:*}"
    minute="${time_input##*:}"
    if [[ "${hour}" -gt 23 || "${minute}" -gt 59 ]]; then
        fail "Invalid time: hour must be 0-23, minute 0-59"
    fi
fi

# --- Build OnCalendar value ---

case "${frequency}" in
    weekly)
        on_calendar="${day} ${time_input}"
        description="every ${day} at ${time_input}"
        ;;
    biweekly)
        on_calendar="${day} *-*-1,15 ${time_input}"
        description="every 2 weeks on ${day} at ${time_input} (1st and 15th)"
        ;;
    monthly)
        on_calendar="*-*-${month_day} ${time_input}"
        description="monthly on day ${month_day} at ${time_input}"
        ;;
    daily)
        on_calendar="${time_input}"
        description="daily at ${time_input}"
        ;;
    custom)
        echo ""
        echo "Enter a systemd OnCalendar expression:"
        echo "  Examples: 'Mon 16:00'  '*-*-1 12:00'  'Mon,Wed,Fri 09:00'"
        echo ""
        read -rp "OnCalendar: " on_calendar
        description="${on_calendar}"
        ;;
esac

# --- Confirm ---

echo ""
echo -e "${YELLOW}New schedule:${NC} ${description}"
echo -e "${YELLOW}OnCalendar:${NC}   ${on_calendar}"
echo ""
read -rp "Apply? (yes/no): " confirm

if [[ "${confirm}" != "yes" && "${confirm}" != "y" ]]; then
    echo "Cancelled."
    exit 0
fi

# --- Apply ---

# Backup before modifying
if [[ -f "${TIMER_FILE}" ]]; then
    cp "${TIMER_FILE}" "${TIMER_FILE}.backup.$(date +%Y%m%d_%H%M%S)"
    ok "Backed up original timer"
fi

cat > "${TIMER_FILE}" <<EOF
[Unit]
Description=Expense Budget Alert Timer -- ${description}

[Timer]
OnCalendar=${on_calendar}
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
ok "Timer file updated"

systemctl restart "${TIMER_NAME}"
ok "Timer restarted"

echo ""
next_run="$(systemctl list-timers "${TIMER_NAME}" --no-pager 2>/dev/null \
    | grep "${TIMER_NAME}" | awk '{print $1, $2, $3}' || true)"
if [[ -n "${next_run}" ]]; then
    echo -e "${GREEN}Next run:${NC} ${next_run}"
fi

echo ""
echo "Schedule: ${description}"
echo ""
echo "Verify:   sudo systemctl list-timers ${TIMER_NAME}"
echo "Test now: sudo systemctl start expense-budget-alert.service"
