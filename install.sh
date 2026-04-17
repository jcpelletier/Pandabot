#!/bin/bash
# install.sh — run as root on the Ubuntu Server to set up the Discord bot.
# Usage: sudo bash install.sh
set -euo pipefail

BOT_DIR="/opt/discord-bot"
BOT_USER="discord-bot"

echo "==> Creating bot user"
if ! id "$BOT_USER" &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$BOT_USER"
fi

# Allow the bot user to read Docker logs (needs to run docker CLI)
# Add to docker group so it can call `docker logs`
usermod -aG docker "$BOT_USER"

echo "==> Creating $BOT_DIR"
mkdir -p "$BOT_DIR"
cp -n "$(dirname "$0")"/*.py "$BOT_DIR/"
cp -n "$(dirname "$0")"/requirements.txt "$BOT_DIR/"
cp -n "$(dirname "$0")"/notify-discord.sh "$BOT_DIR/"
chmod +x "$BOT_DIR/notify-discord.sh"

echo "==> Creating Python venv"
python3 -m venv "$BOT_DIR/venv"
"$BOT_DIR/venv/bin/pip" install --quiet --upgrade pip
"$BOT_DIR/venv/bin/pip" install --quiet -r "$BOT_DIR/requirements.txt"

echo "==> Installing .env"
if [ ! -f "$BOT_DIR/.env" ]; then
  cp "$(dirname "$0")/.env.example" "$BOT_DIR/.env"
  echo ""
  echo "  *** Edit $BOT_DIR/.env before starting the service ***"
  echo ""
fi

echo "==> Setting ownership"
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"
chmod 600 "$BOT_DIR/.env" 2>/dev/null || true

echo "==> Installing systemd service"
cp "$(dirname "$0")/discord-bot.service" /etc/systemd/system/discord-bot.service
systemctl daemon-reload

echo ""
echo "Done.  Next steps:"
echo "  1. Fill in $BOT_DIR/.env"
echo "  2. sudo systemctl enable --now discord-bot"
echo "  3. sudo journalctl -fu discord-bot   # to watch logs"
