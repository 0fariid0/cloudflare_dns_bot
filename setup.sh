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

info() { echo -e "${BLUE}[INFO] $*${NC}"; }
success() { echo -e "${GREEN}[OK] $*${NC}"; }
warn() { echo -e "${YELLOW}[WARN] $*${NC}"; }
fail() { echo -e "${RED}[ERROR] $*${NC}" >&2; pause; }

pause() { echo ""; read -r -p "Press Enter to return to the menu..." _ || true; }

require_root() {
  [[ "${EUID}" -eq 0 ]] || { echo "[ERROR] This script must be run as root." >&2; exit 1; }
}

install_bootstrap_dependencies() {
  command -v apt-get >/dev/null 2>&1 || { echo "[ERROR] Only Ubuntu/Debian systems are supported." >&2; exit 1; }
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y git curl ca-certificates python3 python3-venv python3-pip
}

show_menu() {
  clear || true
  echo "+------------------------------------------+"
  echo "|        Cloudflare DNS Bot Manager        |"
  echo "+------------------------------------------+"
  echo "1) Safe install / reinstall"
  echo "2) Edit config"
  echo "3) Update from GitHub"
  echo "4) Restart service"
  echo "5) Service status"
  echo "6) Live logs"
  echo "7) Create backup"
  echo "8) Full uninstall"
  echo "0) Exit"
  echo ""
  read -r -p "Select an option: " choice
}

runtime_files=(
  "config.py"
  "users.json"
  "blocked_users.json"
  "access_requests.json"
  "smart_connect_ips.json"
  "smart_connect_settings.json"
  "bot_audit.log"
  "dns_advanced.sqlite3"
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
      if [[ "$file" == "dns_advanced.sqlite3" ]]; then
        # Safe SQLite snapshot even while the bot is running and WAL is active.
        if ! python3 - "$source_dir/$file" "$backup_dir/$file" <<'PY_SQLITE'
import sqlite3, sys
source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY_SQLITE
        then
          echo "[ERROR] SQLite backup failed: $source_dir/$file" >&2
          return 1
        fi
      else
        cp -a "$source_dir/$file" "$backup_dir/"
      fi
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
  info "Starting safe install..."
  install_bootstrap_dependencies

  local backup_dir=""
  if [[ -d "$INSTALL_DIR" ]]; then
    backup_dir="$(create_backup "$INSTALL_DIR" || true)"
    if [[ -f "$INSTALL_DIR/dns_advanced.sqlite3" && ( -z "$backup_dir" || ! -f "$backup_dir/dns_advanced.sqlite3" ) ]]; then
      echo "[ERROR] DNS group/history database backup failed. Installation aborted." >&2
      return 1
    fi
    [[ -n "$backup_dir" ]] && success "Previous config backup: $backup_dir"
  fi

  safe_stop_service
  rm -rf "$INSTALL_DIR"
  git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  restore_backup "$backup_dir"
  run_installer
  success "Installation completed."
  pause
}

configure_bot() {
  local config_file="$INSTALL_DIR/config.py"
  if [[ ! -f "$config_file" ]]; then
    fail "config.py was not found. Install the bot first."
    return
  fi

  ${EDITOR:-nano} "$config_file"
  chmod 600 "$config_file" 2>/dev/null || true
  systemctl restart "$SERVICE_NAME" || warn "Service restart failed."
  success "Config saved and service restarted."
  pause
}

update_bot() {
  if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    fail "Git repository was not found. Install the bot first."
    return
  fi

  info "Starting safe update..."
  install_bootstrap_dependencies

  local backup_dir="$(create_backup "$INSTALL_DIR" || true)"
  if [[ -f "$INSTALL_DIR/dns_advanced.sqlite3" && ( -z "$backup_dir" || ! -f "$backup_dir/dns_advanced.sqlite3" ) ]]; then
    echo "[ERROR] DNS database backup failed. Update aborted." >&2
    return 1
  fi
  [[ -n "$backup_dir" ]] && success "Backup before update: $backup_dir"

  cd "$INSTALL_DIR" || return 1
  local old_commit
  old_commit="$(git rev-parse HEAD)" || return 1
  # Fetch *before* stopping the running bot. A network error must not cause downtime.
  if ! git fetch origin "$BRANCH"; then
    warn "Fetch failed. Existing bot was not changed."
    return 1
  fi

  safe_stop_service
  if git reset --hard "origin/${BRANCH}" && restore_backup "$backup_dir" && run_installer; then
    success "Update completed."
    pause
    return 0
  fi

  echo "[ERROR] Update failed; attempting to restore the prior source and runtime data." >&2
  git reset --hard "$old_commit" || warn "Could not restore prior commit automatically."
  restore_backup "$backup_dir" || warn "Could not restore runtime backup automatically."
  run_installer || warn "Could not restart the previous bot; examine systemctl and journalctl."
  return 1
}

restart_service() {
  systemctl restart "$SERVICE_NAME" && success "Service restarted." || warn "Service restart failed."
  pause
}

service_status() {
  systemctl status "$SERVICE_NAME" --no-pager || true
  pause
}

live_logs() {
  echo "Press Ctrl+C to exit."
  journalctl -u "$SERVICE_NAME" -f --no-pager
}

manual_backup() {
  local backup_dir="$(create_backup "$INSTALL_DIR" || true)"
  if [[ -n "$backup_dir" ]]; then
    success "Backup created: $backup_dir"
  else
    warn "No runtime files were found to back up."
  fi
  pause
}

uninstall_bot() {
  warn "This will remove the service and install directory. Important runtime files will be backed up first."
  read -r -p "Type DELETE to confirm: " confirm
  [[ "$confirm" == "DELETE" ]] || { warn "Cancelled."; pause; return; }

  local backup_dir="$(create_backup "$INSTALL_DIR" || true)"
  if [[ -f "$INSTALL_DIR/dns_advanced.sqlite3" && ( -z "$backup_dir" || ! -f "$backup_dir/dns_advanced.sqlite3" ) ]]; then
    echo "[ERROR] DNS database backup failed. Uninstall aborted." >&2
    return 1
  fi
  [[ -n "$backup_dir" ]] && success "Backup before uninstall: $backup_dir"

  safe_stop_service
  systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload
  rm -rf "$INSTALL_DIR"
  success "Bot uninstalled."
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
      0) echo "Exit"; exit 0 ;;
      *) warn "Invalid option."; sleep 1 ;;
    esac
  done
}

main "$@"
