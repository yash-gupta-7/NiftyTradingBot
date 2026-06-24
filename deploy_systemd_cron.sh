#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/ubuntu/trading_agent"
VENV_PYTHON="${PROJECT_ROOT}/venv/bin/python"
RUN_USER="ubuntu"
SYSTEMCTL="/usr/bin/systemctl"

DASHBOARD_SCRIPT="${PROJECT_ROOT}/dashboard.py"
AGENT_SCRIPT="${PROJECT_ROOT}/main.py"
REFRESH_SCRIPT="${PROJECT_ROOT}/refresh_groww_token.py"
ENV_FILE="${PROJECT_ROOT}/.env"

DASHBOARD_SERVICE="/etc/systemd/system/nifty-dashboard.service"
AGENT_SERVICE="/etc/systemd/system/nifty-agent.service"
SUDOERS_FILE="/etc/sudoers.d/nifty-agent-systemctl"

require_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "Missing required file: ${path}" >&2
    exit 1
  fi
}

require_executable() {
  local path="$1"
  if [[ ! -x "${path}" ]]; then
    echo "Missing executable: ${path}" >&2
    exit 1
  fi
}

require_file "${DASHBOARD_SCRIPT}"
require_file "${AGENT_SCRIPT}"
require_file "${REFRESH_SCRIPT}"
require_file "${ENV_FILE}"
require_executable "${VENV_PYTHON}"
command -v visudo >/dev/null
require_executable "${SYSTEMCTL}"

sudo tee "${DASHBOARD_SERVICE}" >/dev/null <<SERVICE
[Unit]
Description=Nifty Trading Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${PROJECT_ROOT}
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_PYTHON} ${DASHBOARD_SCRIPT}
Restart=on-failure
RestartSec=5
StandardOutput=append:${PROJECT_ROOT}/dashboard.log
StandardError=append:${PROJECT_ROOT}/dashboard.log

[Install]
WantedBy=multi-user.target
SERVICE

sudo tee "${AGENT_SERVICE}" >/dev/null <<SERVICE
[Unit]
Description=Nifty Trading Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${PROJECT_ROOT}
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_PYTHON} ${AGENT_SCRIPT}
Restart=on-failure
RestartSec=10
StandardOutput=append:${PROJECT_ROOT}/agent.log
StandardError=append:${PROJECT_ROOT}/agent.log

[Install]
WantedBy=multi-user.target
SERVICE

sudo tee "${SUDOERS_FILE}" >/dev/null <<SUDOERS
ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl start nifty-agent, /usr/bin/systemctl stop nifty-agent
SUDOERS
sudo chmod 0440 "${SUDOERS_FILE}"
sudo visudo -cf "${SUDOERS_FILE}"

sudo "${SYSTEMCTL}" daemon-reload
sudo "${SYSTEMCTL}" enable --now nifty-dashboard.service
sudo "${SYSTEMCTL}" disable nifty-agent.service >/dev/null 2>&1 || true

tmp_cron="$(mktemp)"
crontab -l 2>/dev/null | grep -v -F "# nifty-trading-agent" > "${tmp_cron}" || true
cat >> "${tmp_cron}" <<CRON
10 9 * * 1-5 cd ${PROJECT_ROOT} && ${VENV_PYTHON} ${REFRESH_SCRIPT} >> ${PROJECT_ROOT}/token_refresh.log 2>&1 # nifty-trading-agent
15 9 * * 1-5 sudo /usr/bin/systemctl start nifty-agent >> ${PROJECT_ROOT}/agent.log 2>&1 # nifty-trading-agent
30 15 * * 1-5 sudo /usr/bin/systemctl stop nifty-agent >> ${PROJECT_ROOT}/agent.log 2>&1 # nifty-trading-agent
CRON
crontab "${tmp_cron}"
rm -f "${tmp_cron}"

touch "${PROJECT_ROOT}/dashboard.log" "${PROJECT_ROOT}/agent.log" "${PROJECT_ROOT}/token_refresh.log"
sudo chown "${RUN_USER}:${RUN_USER}" "${PROJECT_ROOT}/dashboard.log" "${PROJECT_ROOT}/agent.log" "${PROJECT_ROOT}/token_refresh.log"

echo
echo "daemon-reload completed."
echo
echo "Dashboard status:"
sudo "${SYSTEMCTL}" status nifty-dashboard.service --no-pager
echo
echo "Agent enabled state (expected: disabled):"
sudo "${SYSTEMCTL}" is-enabled nifty-agent.service || true
echo
echo "Installed cron:"
crontab -l
echo
echo "Log files:"
ls -l "${PROJECT_ROOT}/dashboard.log" "${PROJECT_ROOT}/agent.log" "${PROJECT_ROOT}/token_refresh.log"
