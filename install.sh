#!/usr/bin/env bash

set -Eeuo pipefail

APP_NAME="Cloudflare DNS Telegram Bot"
SERVICE_NAME="cloudflarebot"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
REUSE_CONFIG=false

if [[ "${1:-}" == "--reuse-config" ]]; then
  REUSE_CONFIG=true
fi

cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}ℹ️  $*${NC}"; }
success() { echo -e "${GREEN}✅ $*${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }
fail() { echo -e "${RED}❌ $*${NC}" >&2; exit 1; }

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "این اسکریپت باید با کاربر root اجرا شود."
}

require_debian_like() {
  command -v apt-get >/dev/null 2>&1 || fail "این نصب‌کننده فقط برای Ubuntu/Debian آماده شده است."
}

backup_file() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  local backup="${file}.bak.$(date +%Y%m%d-%H%M%S)"
  cp -a "$file" "$backup"
  chmod 600 "$backup" 2>/dev/null || true
  warn "از فایل قبلی بکاپ گرفته شد: $backup"
}

read_secret() {
  local prompt="$1"
  local var_name="$2"
  local value=""
  while [[ -z "$value" ]]; do
    read -r -s -p "$prompt" value
    echo ""
    [[ -n "$value" ]] || warn "این مقدار نمی‌تواند خالی باشد."
  done
  printf -v "$var_name" '%s' "$value"
}

read_plain() {
  local prompt="$1"
  local var_name="$2"
  local value=""
  read -r -p "$prompt" value
  printf -v "$var_name" '%s' "$value"
}

read_admin_id() {
  local value="${ADMIN_ID:-}"
  while [[ ! "$value" =~ ^[0-9]+$ ]]; do
    read -r -p "Enter Admin Telegram numeric ID: " value
    [[ "$value" =~ ^[0-9]+$ ]] || warn "ADMIN_ID باید فقط عدد باشد."
  done
  ADMIN_ID_INPUT="$value"
}

create_config() {
  if [[ "$REUSE_CONFIG" == true && -s config.py ]]; then
    chmod 600 config.py 2>/dev/null || true
    success "از config.py موجود استفاده شد."
    return 0
  fi

  local bot_token="${BOT_TOKEN:-}"
  local cf_api_key="${CLOUDFLARE_API_KEY:-}"
  local cf_email="${CLOUDFLARE_EMAIL:-}"

  echo ""
  info "Cloudflare API Token پیشنهاد می‌شود. اگر Global API Key می‌دهید، ایمیل Cloudflare هم لازم است."
  echo ""

  [[ -n "$bot_token" ]] || read_secret "Enter Bot Token: " bot_token
  [[ -n "$cf_api_key" ]] || read_secret "Enter Cloudflare API Token OR Global API Key: " cf_api_key
  if [[ -z "$cf_email" ]]; then
    read_plain "Enter Cloudflare Email (only for Global API Key; press Enter to skip): " cf_email
  fi
  read_admin_id

  backup_file config.py

  BOT_TOKEN_INPUT="$bot_token" \
  CLOUDFLARE_EMAIL_INPUT="$cf_email" \
  CLOUDFLARE_API_KEY_INPUT="$cf_api_key" \
  ADMIN_ID_INPUT="$ADMIN_ID_INPUT" \
  python3 - <<'PY'
import os
from pathlib import Path

config = f'''# Telegram bot token from BotFather
BOT_TOKEN = {os.environ["BOT_TOKEN_INPUT"]!r}

# Cloudflare credentials:
# - Recommended: API Token with Zone:Read and DNS:Edit permissions.
# - Legacy: Global API Key + CLOUDFLARE_EMAIL.
CLOUDFLARE_EMAIL = {os.environ.get("CLOUDFLARE_EMAIL_INPUT", "")!r}
CLOUDFLARE_API_KEY = {os.environ["CLOUDFLARE_API_KEY_INPUT"]!r}

# Telegram numeric ID of the bot admin (owner)
ADMIN_ID = {int(os.environ["ADMIN_ID_INPUT"])}
'''
Path("config.py").write_text(config, encoding="utf-8")
PY
  chmod 600 config.py
  success "config.py با فرمت امن ساخته شد."
}

install_system_dependencies() {
  export DEBIAN_FRONTEND=noninteractive
  info "نصب/بررسی پکیج‌های سیستمی..."
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip git curl ca-certificates
}

install_python_dependencies() {
  [[ -f requirements.txt ]] || fail "requirements.txt پیدا نشد."
  info "آماده‌سازی محیط مجازی Python..."
  python3 -m venv venv
  ./venv/bin/python -m pip install --upgrade pip setuptools wheel
  ./venv/bin/python -m pip install --no-cache-dir -r requirements.txt
  success "وابستگی‌های Python نصب/آپدیت شد."
}

write_service() {
  info "ساخت سرویس systemd..."
  cat > "$SERVICE_FILE" <<EOF_SERVICE
[Unit]
Description=${APP_NAME}
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${SCRIPT_DIR}/venv/bin/python ${SCRIPT_DIR}/bot.py
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=20
Environment=PYTHONUNBUFFERED=1
User=root

[Install]
WantedBy=multi-user.target
EOF_SERVICE

  chmod 644 "$SERVICE_FILE"
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null
}

start_service() {
  info "راه‌اندازی سرویس..."
  systemctl restart "$SERVICE_NAME"
  sleep 1
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    success "سرویس فعال است."
  else
    warn "سرویس استارت شد ولی فعال دیده نشد. لاگ را بررسی کنید: journalctl -u ${SERVICE_NAME} -n 80 --no-pager"
  fi
}

main() {
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "🚀 ${APP_NAME} Installer"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  require_root
  require_debian_like
  install_system_dependencies
  create_config
  install_python_dependencies
  write_service
  start_service

  echo ""
  success "نصب/آپدیت کامل شد."
  echo "📡 وضعیت:  systemctl status ${SERVICE_NAME}"
  echo "📜 لاگ:    journalctl -u ${SERVICE_NAME} -f"
}

main "$@"
