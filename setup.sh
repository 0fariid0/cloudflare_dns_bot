#!/bin/bash

INSTALL_DIR="/root/cloudflare_dns_bot"
SERVICE_NAME="cloudflarebot"
LOG_DIR="$INSTALL_DIR/logs"

# At the very beginning of setup.sh
if [ -d "$INSTALL_DIR/.git" ]; then
  cd "$INSTALL_DIR" || exit
  # این خط را اضافه می کنیم تا تغییرات محلی را نادیده بگیرد و نسخه اصلی را دریافت کند
  git reset --hard origin/main
  git pull origin main
  cd - || exit
fi

show_menu() {
  clear
  echo "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓"
  echo "┃   ⚙️ Cloudflare DNS Bot Installer     ┃"
  echo "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
  echo "1) 🛠  Install the bot"
  echo "2) ⚙️  Configure the bot"
  echo "3) 🔄 Update the bot"
  echo "4) ❌ Uninstall the bot"
  echo "5) 📜 View logs"
  echo "0) 🚪 Exit"
  echo ""
  read -p "Your choice: " choice
}

install_bot() {
  echo "📦 Installing the bot..."
  rm -rf "$INSTALL_DIR"
  git clone https://github.com/0fariid0/cloudflare_dns_bot.git "$INSTALL_DIR"
  cd "$INSTALL_DIR" || exit
  bash install.sh
  echo "✅ Installation completed successfully."
  read -p "⏎ Press Enter to return to the menu..." _
}

configure_bot() {
  CONFIG_FILE="$INSTALL_DIR/config.py"
  if [ ! -f "$CONFIG_FILE" ]; then
    echo "⚠️ Config file not found. Please install the bot first."
  else
    echo "📝 Opening the config file..."
    sleep 1
    nano "$CONFIG_FILE"
    echo "🔄 Restarting the bot service..."
    systemctl restart "$SERVICE_NAME"
    echo "✅ Configuration saved and bot restarted."
  fi
  read -p "⏎ Press Enter to return to the menu..." _
}

update_bot() {
  if [ ! -d "$INSTALL_DIR/.git" ]; then
    echo "⚠️ Git repository not found. Please install the bot first."
  else
    echo "🔄 Updating the bot to the latest version..."
    cd "$INSTALL_DIR" || exit
    # این خط را اضافه می کنیم تا تغییرات محلی را نادیده بگیرد و نسخه اصلی را دریافت کند
    git reset --hard origin/main
    git pull origin main
    echo "🔄 Restarting the bot service..."
    systemctl restart "$SERVICE_NAME"
    echo "✅ Bot updated and restarted successfully."
  fi
  read -p "⏎ Press Enter to return to the menu..." _
}

view_logs() {
  # make sure log dir exists (for saved exports)
  mkdir -p "$LOG_DIR"

  if ! systemctl status "$SERVICE_NAME" >/dev/null 2>&1; then
    echo "⚠️ سرویس $SERVICE_NAME پیدا نشد یا فعال نیست."
    read -p "⏎ Press Enter to return to the menu..." _
    return
  fi

  while true; do
    clear
    echo "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓"
    echo "┃      View logs for $SERVICE_NAME     ┃"
    echo "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
    echo "1) 📄 Show last 200 lines"
    echo "2) ▶️ Follow live (journalctl -f)"
    echo "3) 🔎 Open with less (paged)"
    echo "4) 💾 Save last 1000 lines to file"
    echo "0) 🔙 Back to main menu"
    echo ""
    read -p "Choose: " lchoice

    case $lchoice in
      1)
        echo "----- Last 200 lines -----"
        journalctl -u "$SERVICE_NAME" -n 200 --no-pager
        echo "--------------------------"
        read -p "⏎ Press Enter to continue..." _
        ;;
      2)
        echo "----- Following logs (Ctrl+C to stop) -----"
        journalctl -u "$SERVICE_NAME" -f
        # when user Ctrl+C, they'll return here
        ;;
      3)
        # pipe to less for paging
        journalctl -u "$SERVICE_NAME" | less
        ;;
      4)
        TIMESTAMP=$(date +"%F_%H%M%S")
        OUTFILE="$LOG_DIR/${SERVICE_NAME}_logs_${TIMESTAMP}.log"
        echo "Saving last 1000 lines to $OUTFILE ..."
        journalctl -u "$SERVICE_NAME" -n 1000 --no-pager > "$OUTFILE"
        echo "✅ Saved to $OUTFILE"
        read -p "⏎ Press Enter to continue..." _
        ;;
      0)
        break
        ;;
      *)
        echo "❌ Invalid option"
        sleep 1
        ;;
    esac
  done
}

uninstall_bot() {
  echo "❌ Uninstalling the bot completely..."
  systemctl stop "$SERVICE_NAME"
  systemctl disable "$SERVICE_NAME"
  rm -f /etc/systemd/system/"$SERVICE_NAME".service
  systemctl daemon-reload
  rm -rf "$INSTALL_DIR"
  echo "✅ Bot and all files have been removed."
  read -p "⏎ Press Enter to return to the menu..." _
}

while true; do
  show_menu
  case $choice in
    1) install_bot ;;
    2) configure_bot ;;
    3) update_bot ;;
    4) uninstall_bot ;;
    5) view_logs ;;
    0) echo "👋 Exiting. Goodbye!"; exit 0 ;;
    *) echo "❌ Invalid option. Please choose a valid one."; sleep 2 ;;
  esac
done
