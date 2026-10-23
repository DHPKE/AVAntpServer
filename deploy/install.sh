#!/bin/sh
# AVAntpServer installer for Debian 13 (Trixie). Run as root on the box:  sudo ./deploy/install.sh
# Installs packages, copies the code to /opt/avantp and disables every WiFi device.
# It does NOT touch the network or the firewall: that is a separate, guarded step (avantp apply).
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
. /etc/os-release
[ "${VERSION_ID:-}" = "13" ] || { echo "this installer targets Debian 13, found ${PRETTY_NAME:-unknown}" >&2; exit 1; }

SRC=$(cd "$(dirname "$0")/.." && pwd)

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq chrony nftables systemd-resolved python3 rfkill openssl iproute2

# code + example config
mkdir -p /opt/avantp /etc/avantp
cp -r "$SRC/avantp" "$SRC/ui" /opt/avantp/
[ -f /etc/avantp/config.json ] || cp "$SRC/examples/config.json" /etc/avantp/config.json.example

# WiFi would be a third path between the two sides: block it now and keep its driver from loading
rfkill block wifi 2>/dev/null || true
for d in /sys/class/net/*/wireless; do
  [ -d "$d" ] || continue
  dev=$(basename "$(dirname "$d")")
  mod=$(basename "$(readlink -f "/sys/class/net/$dev/device/driver/module" 2>/dev/null)" 2>/dev/null || true)
  if [ -n "$mod" ] && [ "$mod" != "." ]; then
    echo "install $mod /bin/false" > "/etc/modprobe.d/avantp-no-wifi-$mod.conf"
    echo "WiFi $dev: driver $mod disabled (effective after reboot)"
  fi
done

echo "installed: $(chronyd --version | head -1), $(nft --version)"
echo "network and firewall unchanged. Next: put your config in /etc/avantp/config.json"
