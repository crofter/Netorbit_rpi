#!/usr/bin/env bash
# Install or update NetOrbit on a Raspberry Pi (Raspberry Pi OS / Debian).
# Run from a checkout of this repository:   sudo ./deploy/install.sh
set -euo pipefail

PREFIX=/opt/netorbit
PORT=${NETORBIT_PORT:-8099}
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo $0" >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip >/dev/null

echo "==> Creating the netorbit service user"
if ! id netorbit >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/netorbit --no-create-home --shell /usr/sbin/nologin netorbit
fi

echo "==> Installing NetOrbit into $PREFIX/venv"
mkdir -p "$PREFIX"
if [ ! -x "$PREFIX/venv/bin/python" ]; then
  python3 -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$SRC"
ln -sf "$PREFIX/venv/bin/netorbit-pi" /usr/local/bin/netorbit-pi

echo "==> Setting up the service"
sed "s/--port 8099/--port $PORT/" "$SRC/deploy/netorbit.service" > /etc/systemd/system/netorbit.service
systemctl daemon-reload
systemctl enable --quiet netorbit
systemctl restart netorbit

sleep 2
if systemctl is-active --quiet netorbit; then
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo
  echo "NetOrbit is running. Open http://${IP:-<this-pi>}:$PORT/ in a browser."
  echo "The first full scan takes about a minute. Logs: journalctl -u netorbit -f"
else
  echo "NetOrbit did not start. See: journalctl -u netorbit -n 50" >&2
  exit 1
fi
