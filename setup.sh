view_logs() {
  # make sure log dir exists (for saved exports)
  mkdir -p "$LOG_DIR"

  # find candidate service units that mention 'cloud' or 'cloudflare'
  echo "🔎 Searching for related systemd units..."
  candidates=$(systemctl list-units --type=service --all --no-legend | awk '{print $1, $2, $3, $4}' | grep -iE 'cloud|cloudflare' || true)

  if [ -z "$candidates" ]; then
    echo "⚠️ هیچ سرویس systemd مرتبط پیدا نشد."
    echo "لیست کامل سرویس‌ها که عبارت cloud یا cloudflare را دارند:"
    systemctl list-units --type=service --all | grep -iE 'cloud|cloudflare' || true
    echo ""
    echo "اگر می‌خواهی هنوز لاگ‌ش را ببینی، نام unit را وارد کن (یا Enter برای بازگشت):"
    read -p "Unit name: " maybe_unit
    if [ -z "$maybe_unit" ]; then
      read -p "⏎ Press Enter to return to the menu..." _
      return
    else
      UNIT="$maybe_unit"
    fi
  else
    echo "🔔 سرویس‌های پیدا شده:"
    echo "$candidates"
    echo ""
    echo "لطفاً نام دقیق unit را وارد کن (مثلاً cloudflarebot.service) یا Enter برای انتخاب اولین مورد:"
    read -p "Unit name: " UNIT
    if [ -z "$UNIT" ]; then
      # pick the first column (unit name) of first candidate
      UNIT=$(echo "$candidates" | head -n1 | awk '{print $1}')
    fi
  fi

  if [ -z "$UNIT" ]; then
    echo "❌ واحدی انتخاب نشده. بازگشت به منو."
    read -p "⏎ Press Enter to return to the menu..." _
    return
  fi

  # permissive: run journalctl even if unit is inactive
  while true; do
    clear
    echo "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓"
    echo "┃      View logs for $UNIT      ┃"
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
        sudo journalctl -u "$UNIT" -n 200 --no-pager || sudo journalctl | grep -i "$UNIT" || true
        echo "--------------------------"
        read -p "⏎ Press Enter to continue..." _
        ;;
      2)
        echo "----- Following logs (Ctrl+C to stop) -----"
        sudo journalctl -u "$UNIT" -f
        ;;
      3)
        # pipe to less for paging (use --no-pager to get full output then less)
        sudo journalctl -u "$UNIT" --no-pager | less
        ;;
      4)
        TIMESTAMP=$(date +"%F_%H%M%S")
        OUTFILE="$LOG_DIR/${UNIT}_logs_${TIMESTAMP}.log"
        echo "Saving last 1000 lines to $OUTFILE ..."
        sudo journalctl -u "$UNIT" -n 1000 --no-pager > "$OUTFILE" 2>/dev/null || sudo journalctl | grep -i "$UNIT" > "$OUTFILE" || true
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
