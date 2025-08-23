#!/bin/bash

INSTALL_DIR="/root/cloudflare_dns_bot"
SERVICE_NAME="cloudflarebot"
# At the very beginning of setup.sh
if [ -d "$INSTALL_DIR/.git" ]; then
  cd "$INSTALL_DIR" || exit
  git reset --hard origin/main
  git pull origin main
  cd - || exit
fi

show_menu() {
  clear
  echo "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓"
  echo "┃   ⚙️ Cloudflare DNS Bot Installer     ┃"
  echo "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
  echo "1) 🛠  Install the bot"
  echo "2) ⚙️  Configure the bot"
  echo "3) 🔄 Update the bot"
  echo "4) ❌ Uninstall the bot"
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
    git reset --hard origin/main
    git pull origin main
    # Reinstall dependencies in case they've changed
    if [ -f "requirements.txt" ]; then
      echo "📦 Reinstalling dependencies from requirements.txt..."
      source venv/bin/activate
      pip install -r requirements.txt
      deactivate
    fi
    echo "🔄 Restarting the bot service..."
    systemctl restart "$SERVICE_NAME"
    echo "✅ Bot updated and restarted successfully."
  fi
  read -p "⏎ Press Enter to return to the menu..." _
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
    0) echo "👋 Exiting. Goodbye!"; exit 0 ;;
    *) echo "❌ Invalid option. Please choose a valid one."; sleep 2 ;;
  esac
done
