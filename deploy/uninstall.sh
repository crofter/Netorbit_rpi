#!/usr/bin/env bash
# Remove NetOrbit. Your map settings in /var/lib/netorbit are kept unless you pass --purge.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "Please run with sudo" >&2; exit 1; fi
systemctl disable --now netorbit 2>/dev/null || true
rm -f /etc/systemd/system/netorbit.service /usr/local/bin/netorbit-pi
systemctl daemon-reload
rm -rf /opt/netorbit
if [ "${1:-}" = "--purge" ]; then
  rm -rf /var/lib/netorbit /var/lib/private/netorbit
  userdel netorbit 2>/dev/null || true
  echo "NetOrbit and its data removed."
else
  echo "NetOrbit removed. Settings kept in /var/lib/netorbit (use --purge to delete them)."
fi
