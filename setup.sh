#!/usr/bin/env bash

set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/0fariid0/cloudflare_dns_bot.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/root/cloudflare_dns_bot}"
SERVICE_NAME="cloudflarebot"
BACKUP_ROOT="/root/cloudflare_dns_bot_backups"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}ℹ️  $*${NC}"; }
success() { echo -e "${GREEN}✅ $*${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }
fail() { echo -e "${RED}❌ $*${NC}" >&2; pause; }

pause() { echo ""; read -r -p "⏎ برای بازگشت به منو Enter بزنید..." _ || true; }

require_root() {
  [[ "${EUID}" -eq 0 ]] || { echo "❌ این اسکریپت باید با کاربر root اجرا شود." >&2; exit 1; }
}

install_bootstrap_dependencies() {
  command -v apt-get >/dev/null 2>&1 || { echo "❌ فقط Ubuntu/Debian پشتیبانی می‌شود." >&2; exit 1; }
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y git curl ca-certificates python3 python3-venv python3-pip
}

show_menu() {
  clear
  echo "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓"
  echo "┃      ⚙️  Cloudflare DNS Bot Manager        ┃"
  echo "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
  echo "1) 🛠  نصب / نصب مجدد امن"
  echo "2) ⚙️  ویرایش تنظیمات"
  echo "3) 🔄 آپدیت از گیت‌هاب"
  echo "4) ♻️  ریستارت سرویس"
  echo "5) 📡 وضعیت سرویس"
  echo "6) 📜 نمایش لاگ زنده"
  echo "7) 💾 گرفتن بکاپ"
  echo "8) ❌ حذف کامل"
  echo "0) خروج"
  echo ""
  read -r -p "انتخاب شما: " choice
}

runtime_files=(
  "config.py"
  "users.json"
  "blocked_users.json"
  "access_requests.json"
  "smart_connect_ips.json"
  "smart_connect_settings.json"
  "bot_audit.log"
)

create_backup() {
  local source_dir="${1:-$INSTALL_DIR}"
  [[ -d "$source_dir" ]] || return 1
  mkdir -p "$BACKUP_ROOT"
  local backup_dir="${BACKUP_ROOT}/backup-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$backup_dir"

  local copied=0
  for file in "${runtime_files[@]}"; do
    if [[ -f "$source_dir/$file" ]]; then
      cp -a "$source_dir/$file" "$backup_dir/"
      copied=$((copied + 1))
    fi
  done

  if [[ "$copied" -gt 0 ]]; then
    chmod -R go-rwx "$backup_dir" 2>/dev/null || true
    echo "$backup_dir"
    return 0
  fi

  rm -rf "$backup_dir"
  return 1
}

restore_backup() {
  local backup_dir="$1"
  [[ -n "$backup_dir" && -d "$backup_dir" ]] || return 0
  mkdir -p "$INSTALL_DIR"
  for file in "${runtime_files[@]}"; do
    if [[ -f "$backup_dir/$file" ]]; then
      cp -a "$backup_dir/$file" "$INSTALL_DIR/"
    fi
  done
  chmod 600 "$INSTALL_DIR/config.py" 2>/dev/null || true
}

safe_stop_service() {
  systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
}

run_installer() {
  cd "$INSTALL_DIR" || return 1
  if [[ -s config.py ]]; then
    bash install.sh --reuse-config
  else
    bash install.sh
  fi
}

install_bot() {
  info "شروع نصب امن..."
  install_bootstrap_dependencies

  local backup_dir=""
  if [[ -d "$INSTALL_DIR" ]]; then
    backup_dir="$(create_backup "$INSTALL_DIR" || true)"
    [[ -n "$backup_dir" ]] && success "بکاپ تنظیمات قبلی: $backup_dir"
  fi

  safe_stop_service
  rm -rf "$INSTALL_DIR"
  git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  restore_backup "$backup_dir"
  run_installer
  success "نصب انجام شد."
  pause
}

configure_bot() {
  local config_file="$INSTALL_DIR/config.py"
  if [[ ! -f "$config_file" ]]; then
    fail "config.py پیدا نشد. اول ربات را نصب کنید."
    return
  fi

  ${EDITOR:-nano} "$config_file"
  chmod 600 "$config_file" 2>/dev/null || true
  systemctl restart "$SERVICE_NAME" || warn "ریستارت سرویس ناموفق بود."
  success "تنظیمات ذخیره شد و سرویس ریستارت شد."
  pause
}

update_bot() {
  if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    fail "مخزن git پیدا نشد. اول نصب را انجام دهید."
    return
  fi

  info "شروع آپدیت امن..."
  install_bootstrap_dependencies

  local backup_dir="$(create_backup "$INSTALL_DIR" || true)"
  [[ -n "$backup_dir" ]] && success "بکاپ قبل از آپدیت: $backup_dir"

  cd "$INSTALL_DIR" || return 1
  git fetch origin "$BRANCH"
  git reset --hard "origin/${BRANCH}"
  restore_backup "$backup_dir"
  run_installer
  success "آپدیت کامل شد."
  pause
}

restart_service() {
  systemctl restart "$SERVICE_NAME" && success "سرویس ریستارت شد." || warn "ریستارت سرویس ناموفق بود."
  pause
}

service_status() {
  systemctl status "$SERVICE_NAME" --no-pager || true
  pause
}

live_logs() {
  echo "برای خروج Ctrl+C بزنید."
  journalctl -u "$SERVICE_NAME" -f --no-pager
}

manual_backup() {
  local backup_dir="$(create_backup "$INSTALL_DIR" || true)"
  if [[ -n "$backup_dir" ]]; then
    success "بکاپ ساخته شد: $backup_dir"
  else
    warn "فایل قابل بکاپی پیدا نشد."
  fi
  pause
}

uninstall_bot() {
  warn "این کار سرویس و پوشه نصب را حذف می‌کند. فایل‌های مهم قبل از حذف بکاپ می‌شوند."
  read -r -p "برای تایید عبارت DELETE را بنویسید: " confirm
  [[ "$confirm" == "DELETE" ]] || { warn "لغو شد."; pause; return; }

  local backup_dir="$(create_backup "$INSTALL_DIR" || true)"
  [[ -n "$backup_dir" ]] && success "بکاپ قبل از حذف: $backup_dir"

  safe_stop_service
  systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload
  rm -rf "$INSTALL_DIR"
  success "ربات حذف شد."
  pause
}

main() {
  require_root
  while true; do
    show_menu
    case "${choice:-}" in
      1) install_bot ;;
      2) configure_bot ;;
      3) update_bot ;;
      4) restart_service ;;
      5) service_status ;;
      6) live_logs ;;
      7) manual_backup ;;
      8) uninstall_bot ;;
      0) echo "خروج"; exit 0 ;;
      *) warn "گزینه نامعتبر است."; sleep 1 ;;
    esac
  done
}

main "$@"
